from functools import partial

from effect import (
    ComposedDispatcher, Constant, Effect, Error, Func, ParallelEffects,
    TypeDispatcher, base_dispatcher, sync_perform)
from effect.async import perform_parallel_async
from effect.ref import Reference, reference_dispatcher
from effect.testing import EQDispatcher, EQFDispatcher

import mock

from pyrsistent import freeze, pmap, pset, s

from twisted.internet.defer import fail, succeed
from twisted.trial.unittest import SynchronousTestCase

from otter.constants import CONVERGENCE_DIRTY_DIR, ServiceType
from otter.convergence.model import (
    CLBDescription, CLBNode, NovaServer, ServerState, StepResult)
from otter.convergence.service import (
    ConcurrentError,
    ConvergenceStarter,
    Converger,
    converge_all,
    converge_one,
    determine_active, execute_convergence, get_my_divergent_groups,
    make_lock_set,
    non_concurrently)
from otter.http import TenantScope, service_request
from otter.models.intents import (
    GetScalingGroupInfo, ModifyGroupState, perform_modify_group_state)
from otter.models.interface import GroupState, NoSuchScalingGroupError
from otter.test.convergence.test_planning import server
from otter.test.util.test_zk import ZNodeStatStub
from otter.test.utils import (
    CheckFailureValue, FakePartitioner, IsBoundWith,
    matches,
    mock_group, mock_log,
    transform_eq)
from otter.util.zk import CreateOrSet, DeleteNode, GetChildrenWithStats


class ConvergenceStarterTests(SynchronousTestCase):
    """Tests for :obj:`ConvergenceStarter`."""

    def test_start_convergence(self):
        """Starting convergence marks dirty and logs a message."""
        svc = ConvergenceStarter('my-dispatcher')
        log = mock_log()

        def perform(dispatcher, eff):
            return succeed((dispatcher, eff))
        d = svc.start_convergence(log, 'tenant', 'group', perform=perform)
        self.assertEqual(
            self.successResultOf(d),
            ('my-dispatcher',
             Effect(CreateOrSet(path='/groups/divergent/tenant_group',
                                content='dirty'))))
        log.msg.assert_called_once_with(
            'mark-dirty-success', tenant_id='tenant', group_id='group')

    def test_error_marking_dirty(self):
        """An error is logged when marking dirty fails."""
        svc = ConvergenceStarter('my-dispatcher')
        log = mock_log()

        def perform(dispatcher, eff):
            return fail(RuntimeError('oh no'))
        d = svc.start_convergence(log, 'tenant', 'group', perform=perform)
        self.assertEqual(self.successResultOf(d), None)
        log.err.assert_called_once_with(
            CheckFailureValue(RuntimeError('oh no')),
            'mark-dirty-failure', tenant_id='tenant', group_id='group')


class ConvergerTests(SynchronousTestCase):
    """Tests for :obj:`Converger`."""

    def setUp(self):
        self.dispatcher = _get_dispatcher()
        self.log = mock_log()
        self.buckets = range(10)

    def _pfactory(self, log, callable):
        self.fake_partitioner = FakePartitioner(log, callable)
        return self.fake_partitioner

    def test_buckets_acquired(self):
        """
        When buckets are allocated, the result of converge_all is performed.
        """
        def converge_all(log, group_locks, _my_buckets, all_buckets):
            self.assertEqual(log, matches(IsBoundWith(system='converger')))
            self.assertIs(group_locks, converger.group_locks)
            self.assertEqual(_my_buckets, my_buckets)
            self.assertEqual(all_buckets, self.buckets)
            return Effect(Constant('foo'))

        my_buckets = [0, 5]
        converger = Converger(
            self.log, self.dispatcher, self.buckets,
            self._pfactory, converge_all=converge_all)

        result = self.fake_partitioner.got_buckets(my_buckets)
        self.assertEqual(self.successResultOf(result), 'foo')
        self.log.msg.assert_called_once_with(
            'buckets-acquired', my_buckets=[0, 5], system='converger')

    def test_buckets_acquired_errors(self):
        """
        Errors raised from performing the converge_all effect are logged, and
        None is the ultimate result.
        """
        def converge_all(log, group_locks, _my_buckets, all_buckets):
            return Effect(Error(RuntimeError('foo')))

        Converger(
            self.log, self.dispatcher, self.buckets,
            self._pfactory, converge_all=converge_all)

        result = self.fake_partitioner.got_buckets([0])
        self.assertEqual(self.successResultOf(result), None)
        self.log.err.assert_called_once_with(
            CheckFailureValue(RuntimeError('foo')),
            'converge-all-error', system='converger')


class ConvergeOneTests(SynchronousTestCase):

    def setUp(self):
        self.log = mock_log()
        self.tenant_id = 'tenant-id'
        self.group_id = 'g1'
        self.version = 5
        self.deletions = []
        self.dispatcher = ComposedDispatcher([
            EQFDispatcher([
                (DeleteNode(path='/groups/divergent/tenant-id_g1',
                            version=self.version),
                 lambda i: self.deletions.append(True))
            ]),
            _get_dispatcher(),
        ])

    def test_success(self):
        """
        runs execute_convergence and returns None, then deletes the dirty flag.
        """
        calls = []

        def execute_convergence(tenant_id, group_id, log):
            return Effect(Func(
                lambda: calls.append((tenant_id, group_id, log))))

        eff = converge_one(
            self.log, make_lock_set(), self.tenant_id, self.group_id,
            self.version,
            execute_convergence=execute_convergence)
        result = sync_perform(self.dispatcher, eff)
        self.assertEqual(result, None)
        self.assertEqual(
            calls,
            [(self.tenant_id, self.group_id,
              matches(IsBoundWith(tenant_id=self.tenant_id,
                                  group_id=self.group_id)))])
        self.assertEqual(self.deletions, [True])
        self.log.msg.assert_any_call(
            'mark-clean-success',
            tenant_id=self.tenant_id, group_id=self.group_id)

    def test_non_concurrent(self):
        """
        Won't run execute_convergence if it's already running for the same
        group ID.
        """
        calls = []

        def execute_convergence(tenant_id, group_id, log):
            return Effect(Func(lambda: calls.append('should not be run')))

        lock_set = partial(non_concurrently, Reference(pset([self.group_id])))
        eff = converge_one(
            self.log, lock_set, self.tenant_id,
            self.group_id, self.version,
            execute_convergence=execute_convergence)
        result = sync_perform(self.dispatcher, eff)
        self.assertEqual(result, None)
        self.assertEqual(calls, [])

    def test_no_scaling_group(self):
        """
        When the scaling group disappears, a fatal error is logged and the
        dirty flag is cleaned up.
        """
        def execute_convergence(tenant_id, group_id, log):
            return Effect(Error(NoSuchScalingGroupError(tenant_id, group_id)))

        eff = converge_one(
            self.log, make_lock_set(), self.tenant_id, self.group_id,
            self.version,
            execute_convergence=execute_convergence)
        result = sync_perform(self.dispatcher, eff)
        self.assertEqual(result, None)

        self.log.err.assert_any_call(
            CheckFailureValue(NoSuchScalingGroupError(self.tenant_id,
                                                      self.group_id)),
            'converge-fatal-error',
            tenant_id=self.tenant_id, group_id=self.group_id)

        self.assertEqual(self.deletions, [True])
        self.log.msg.assert_any_call(
            'mark-clean-success',
            tenant_id=self.tenant_id, group_id=self.group_id)

    def test_concurrent_error_ignored(self):
        """
        ConcurrentError does not cause a log message, and doesn't clean up the
        dirty flag.
        """
        def execute_convergence(tenant_id, group_id, log):
            return Effect(Error(ConcurrentError(group_id)))

        eff = converge_one(
            self.log, make_lock_set(), self.tenant_id, self.group_id,
            self.version,
            execute_convergence=execute_convergence)
        result = sync_perform(self.dispatcher, eff)
        self.assertEqual(result, None)
        self.assertEqual(self.log.err.mock_calls, [])
        self.assertEqual(self.deletions, [])

    def test_unexpected_errors(self):
        """
        Unexpected exceptions log a non-fatal error and don't clean up the
        dirty flag.
        """
        def execute_convergence(tenant_id, group_id, log):
            return Effect(Error(RuntimeError('uh oh!')))

        eff = converge_one(
            self.log, make_lock_set(), self.tenant_id, self.group_id,
            self.version,
            execute_convergence=execute_convergence)
        result = sync_perform(self.dispatcher, eff)
        self.assertEqual(result, None)
        self.log.err.assert_any_call(
            CheckFailureValue(RuntimeError('uh oh!')),
            'converge-non-fatal-error',
            tenant_id=self.tenant_id, group_id=self.group_id)
        self.assertEqual(self.deletions, [])


class ConvergeAllTests(SynchronousTestCase):
    """Tests for :func:`converge_all`."""

    def test_converge_all(self):
        """
        Fetches divergent groups and runs converge_one for each one needing
        convergence.
        """
        def get_my_divergent_groups(_my_buckets, _all_buckets):
            self.assertEqual(_my_buckets, my_buckets)
            self.assertEqual(_all_buckets, all_buckets)
            return Effect(Constant([
                {'tenant_id': '00', 'group_id': 'g1', 'version': 1},
                {'tenant_id': '01', 'group_id': 'g2', 'version': 5}
            ]))

        def converge_one(log, lock_set, tenant_id, group_id, version):
            return Effect(Constant(
                (tenant_id, group_id, version, 'converge!')))

        log = mock_log()
        lock_set = make_lock_set()
        my_buckets = [0, 5]
        all_buckets = range(10)
        result = converge_all(log, lock_set, my_buckets, all_buckets,
                              get_my_divergent_groups=get_my_divergent_groups,
                              converge_one=converge_one)
        self.assertEqual(
            sync_perform(_get_dispatcher(), result),
            [('00', 'g1', 1, 'converge!'),
             ('01', 'g2', 5, 'converge!')])
        log.msg.assert_called_once_with(
            'converge-all',
            group_infos=[{'tenant_id': '00', 'group_id': 'g1', 'version': 1},
                         {'tenant_id': '01', 'group_id': 'g2', 'version': 5}])


class GetMyDivergentGroupsTests(SynchronousTestCase):

    def test_get_my_divergent_groups(self):
        """
        :func:`get_my_divergent_groups` gets information about divergent groups
        that are associated with the given buckets.
        """
        # sha1('00') % 10 is 6, sha1('01') % 10 is 1.
        dispatcher = ComposedDispatcher([
            EQDispatcher([
                (GetChildrenWithStats(CONVERGENCE_DIRTY_DIR),
                 [('00_gr1', ZNodeStatStub(version=0)),
                  ('00_gr2', ZNodeStatStub(version=3)),
                  ('01_gr3', ZNodeStatStub(version=5))]),
            ]),
            _get_dispatcher()
        ])
        result = sync_perform(
            dispatcher, get_my_divergent_groups([6], range(10)))
        self.assertEqual(
            result,
            [{'tenant_id': '00', 'group_id': 'gr1', 'version': 0},
             {'tenant_id': '00', 'group_id': 'gr2', 'version': 3}])


def _get_dispatcher():
    return ComposedDispatcher([
        TypeDispatcher({
            ParallelEffects: perform_parallel_async,
        }),
        reference_dispatcher,
        base_dispatcher,
    ])


class MakeLockSetTests(SynchronousTestCase):
    """Tests for :func:`make_lock_set`."""

    def test_make_lock_set(self):
        """
        Returns a :obj:`Reference` to an empty :obj:`PSet` partially applied to
        :func:`non_concurrently`.
        """
        lock_set = make_lock_set()
        self.assertIs(lock_set.func, non_concurrently)
        self.assertEqual(lock_set.keywords, None)
        (ref,) = lock_set.args
        self.assertEqual(sync_perform(_get_dispatcher(), ref.read()),
                         pset())


class NonConcurrentlyTests(SynchronousTestCase):
    """Tests for :func:`non_concurrently`."""

    def setUp(self):
        self.locks = Reference(pset())

    def _get_locks(self):
        """Get the locks set."""
        return sync_perform(_get_dispatcher(), self.locks.read())

    def _add_lock(self, value):
        """Add an item to the locks set."""
        return sync_perform(_get_dispatcher(),
                            self.locks.modify(lambda cc: cc.add(value)))

    def test_success(self):
        """
        :func:`non_concurrently` returns the result of the passed effect, and
        adds the ``key`` to the ``locks`` while executing.
        """
        dispatcher = _get_dispatcher()

        def execute_stuff():
            self.assertEqual(self._get_locks(), pset(['the-key']))
            return 'foo'

        eff = Effect(Func(execute_stuff))

        non_c_eff = non_concurrently(self.locks, 'the-key', eff)
        self.assertEqual(sync_perform(dispatcher, non_c_eff), 'foo')
        # after the effect completes, its lock is released
        self.assertEqual(self._get_locks(), pset([]))

    def test_refuses_concurrency(self):
        """
        :func:`non_concurrently` raises :obj:`ConcurrentError` when the key is
        already locked.
        """
        self._add_lock('the-key')
        eff = Effect(Error(RuntimeError('foo')))
        non_c_eff = non_concurrently(self.locks, 'the-key', eff)
        self.assertRaises(
            ConcurrentError,
            sync_perform, _get_dispatcher(), non_c_eff)
        self.assertEqual(self._get_locks(), pset(['the-key']))

    def test_cleans_up_on_exception(self):
        """
        When the effect results in error, the key is still removed from the
        locked set.
        """
        dispatcher = _get_dispatcher()
        eff = Effect(Error(RuntimeError('foo!')))
        non_c_eff = non_concurrently(self.locks, 'the-key', eff)
        e = self.assertRaises(RuntimeError, sync_perform, dispatcher,
                              non_c_eff)
        self.assertEqual(str(e), 'foo!')
        self.assertEqual(self._get_locks(), pset([]))


class ExecuteConvergenceTests(SynchronousTestCase):
    """Tests for :func:`execute_convergence`."""

    def setUp(self):
        self.tenant_id = 'tenant-id'
        self.group_id = 'group-id'
        self.state = GroupState(self.tenant_id, self.group_id, 'group-name',
                                {}, {}, None, {}, False, desired=2)
        self.group = mock_group(self.state, self.tenant_id, self.group_id)
        self.lc = {'args': {'server': {'name': 'foo'}, 'loadBalancers': []}}
        self.desired_lbs = s(CLBDescription(lb_id='23', port=80))
        self.servers = [
            NovaServer(id='a',
                       state=ServerState.ACTIVE,
                       created=0,
                       image_id='image',
                       flavor_id='flavor',
                       servicenet_address='10.0.0.1',
                       desired_lbs=self.desired_lbs,
                       links=freeze([{'href': 'link1', 'rel': 'self'}])),
            NovaServer(id='b',
                       state=ServerState.ACTIVE,
                       created=0,
                       image_id='image',
                       flavor_id='flavor',
                       servicenet_address='10.0.0.2',
                       desired_lbs=self.desired_lbs,
                       links=freeze([{'href': 'link2', 'rel': 'self'}]))
        ]
        gsgi = GetScalingGroupInfo(tenant_id='tenant-id',
                                   group_id='group-id')
        manifest = {  # Many details elided!
            'state': self.state,
            'launchConfiguration': self.lc,
        }
        gsgi_result = (self.group, manifest)
        self.expected_intents = [(gsgi, gsgi_result)]

    def _get_dispatcher(self, expected_intents=None):
        if expected_intents is None:
            expected_intents = self.expected_intents
        return ComposedDispatcher([
            EQDispatcher(expected_intents),
            EQFDispatcher([(TenantScope(mock.ANY, self.tenant_id),
                            lambda tscope: tscope.effect)]),
            TypeDispatcher({
                ParallelEffects: perform_parallel_async,
                ModifyGroupState: perform_modify_group_state,
            }),
            base_dispatcher,
        ])

    def _get_gacd_func(self, group_id):
        def get_all_convergence_data(grp_id):
            self.assertEqual(grp_id, group_id)
            return Effect(Constant((tuple(self.servers), ())))
        return get_all_convergence_data

    def test_no_steps(self):
        """
        If state of world matches desired, no steps are executed, but the
        `active` servers are still updated.
        """
        log = mock_log()
        gacd = self._get_gacd_func(self.group.uuid)
        for serv in self.servers:
            serv.desired_lbs = pset()
        eff = execute_convergence(self.tenant_id, self.group_id, log,
                                  get_all_convergence_data=gacd)
        expected_active = {
            'a': {'id': 'a', 'links': [{'href': 'link1', 'rel': 'self'}]},
            'b': {'id': 'b', 'links': [{'href': 'link2', 'rel': 'self'}]}
        }
        result = sync_perform(self._get_dispatcher(), eff)
        self.assertEqual(self.group.modify_state_values[-1].active,
                         expected_active)
        self.assertEqual(result, [])

    def test_success(self):
        """
        Executes optimized steps if state of world does not match desired and
        returns the result of all the steps.
        """
        # The scenario: We have two servers but they're not in the LBs
        # yet. convergence should add them to the LBs.
        log = mock_log()
        gacd = self._get_gacd_func(self.group.uuid)
        eff = execute_convergence(self.tenant_id, self.group_id, log,
                                  get_all_convergence_data=gacd)
        expected_req = service_request(
            ServiceType.CLOUD_LOAD_BALANCERS,
            'POST',
            'loadbalancers/23/nodes',
            data=transform_eq(
                freeze,
                pmap({
                    'nodes': transform_eq(
                        lambda nodes: set(freeze(nodes)),
                        set([pmap({'weight': 1, 'type': 'PRIMARY',
                                   'port': 80,
                                   'condition': 'ENABLED',
                                   'address': '10.0.0.2'}),
                             pmap({'weight': 1, 'type': 'PRIMARY',
                                   'port': 80,
                                   'condition': 'ENABLED',
                                   'address': '10.0.0.1'})]))})),
            success_pred=mock.ANY)
        expected_intents = self.expected_intents + [
            (expected_req.intent, 'successful response')]
        result = sync_perform(self._get_dispatcher(expected_intents), eff)
        self.assertEqual(self.group.modify_state_values[-1].active, {})
        self.assertEqual(result, [(StepResult.SUCCESS, [])])

    def test_first_error_extraction(self):
        """
        If the GetScalingGroupInfo effect fails, its exception is raised
        directly, without the FirstError wrapper.
        """
        log = mock_log()
        gacd = self._get_gacd_func(self.group.uuid)
        for srv in self.servers:
            srv.desired_lbs = pmap()

        tscope_eff = execute_convergence(self.tenant_id, self.group_id, log,
                                         get_all_convergence_data=gacd)

        # Perform the GetScalingGroupInfo by raising an exception
        dispatcher = ComposedDispatcher([
            EQFDispatcher([
                (GetScalingGroupInfo(
                    tenant_id='tenant-id',
                    group_id='group-id'),
                 lambda i: raise_(RuntimeError('foo')))]),
            self._get_dispatcher()])

        # And make sure that exception isn't wrapped in FirstError.
        e = self.assertRaises(
            RuntimeError,
            sync_perform, dispatcher, tscope_eff.intent.effect)
        self.assertEqual(str(e), 'foo')


def raise_(e):
    raise e


class DetermineActiveTests(SynchronousTestCase):
    """Tests for :func:`determine_active`."""

    def test_nothing(self):
        """No input means no active servers."""
        self.assertEqual(determine_active([], []), [])

    def test_active(self):
        """Built servers with no desired LBs are active."""
        servers = [server('id1', ServerState.ACTIVE),
                   server('id2', ServerState.BUILD)]
        self.assertEqual(determine_active(servers, []), servers[:1])

    def test_lb_pending(self):
        """
        When a server should be in a LB but it's not, it's not active.
        """
        desired_lbs = s(CLBDescription(lb_id='foo', port=80))
        lb_nodes = [
            CLBNode(node_id='x',
                    description=CLBDescription(lb_id='foo', port=80),
                    address='1.1.1.3')]
        servers = [
            server('id1', ServerState.ACTIVE, servicenet_address='1.1.1.1',
                   desired_lbs=desired_lbs),
            server('id2', ServerState.ACTIVE, servicenet_address='1.1.1.2',
                   desired_lbs=desired_lbs),
            server('id3', ServerState.ACTIVE, servicenet_address='1.1.1.3',
                   desired_lbs=desired_lbs)]
        self.assertEqual(determine_active(servers, lb_nodes), servers[2:])

    def test_multiple_lb_pending(self):
        """
        When a server needs to be added to multiple LBs, it's only counted
        once.
        """
        lb_nodes = [
            CLBNode(node_id='1',
                    description=CLBDescription(lb_id='foo', port=1),
                    address='1.1.1.1'),
            CLBNode(node_id='2',
                    description=CLBDescription(lb_id='foo', port=2),
                    address='1.1.1.1'),
            CLBNode(node_id='3',
                    description=CLBDescription(lb_id='bar', port=3),
                    address='1.1.1.1'),
            CLBNode(node_id='4',
                    description=CLBDescription(lb_id='bar', port=4),
                    address='1.1.1.1'),
        ]
        desired_lbs = s(CLBDescription(lb_id='foo', port=1),
                        CLBDescription(lb_id='foo', port=2),
                        CLBDescription(lb_id='bar', port=3),
                        CLBDescription(lb_id='bar', port=4))
        servers = [
            server('id1', ServerState.ACTIVE, servicenet_address='1.1.1.1',
                   desired_lbs=desired_lbs),
            server('id2', ServerState.ACTIVE, servicenet_address='1.1.1.2',
                   desired_lbs=desired_lbs)
        ]
        self.assertEqual(determine_active(servers, lb_nodes), servers[:1])
