"""
Integration-y tests for the REST interface interacting with the mock model.

This is perhaps not the place for these tests to go.  Also, perhaps this should
instead be tested by spinning up an actually HTTP server (thus this test can
happen using the mock tap file).

But until a decision has been made for integration test infrastructure and
frameworks, this will do for now, as it is needed to verify that the rest unit
tests and mock model unit tests do not lie.
"""

import json
from urlparse import urlsplit

from twisted.trial.unittest import TestCase
from twisted.internet import defer, endpoints, reactor

from otter.json_schema.group_examples import (
    config, launch_server_config, policy)
from otter.models.interface import NoSuchPolicyError, NoSuchScalingGroupError
from otter.models.cass import CassScalingGroupCollection
from otter.rest.application import root, set_store

from otter.util import keyspaces

from otter.test.rest.request import request

from silverberg import client


def _strip_base_url(url):
    return urlsplit(url)[2].rstrip('/')


generator = keyspaces.CQLGenerator(keyspaces.schema_dir + '/setup')
try:
    cluster = keyspaces.RunningCassandraCluster(setup_cql=generator.generate_cql)
    cluster.setup_keyspace('cassresttest')
except Exception as e:
    skip = "Cassandra unavailable: {0}".format(e)


class CassStoreRestScalingGroupTestCase(TestCase):
    """
    Test case for testing the REST API for the scaling group specific endpoints
    (not policies or webhooks) against the Cassandra model.
    """

    _config = config()[1]
    _launch_server_config = launch_server_config()[0]
    _policies = policy()

    client = None

    def setUp(self):
        """
        Set up a silverberg client
        """
        if self.client is None:
            # only do this once
            self.client = client.CQLClient(
                endpoints.clientFromString(reactor, "tcp:localhost:9160"),
                "cassresttest")
            store = CassScalingGroupCollection(self.client)
            set_store(store)

    def tearDown(self):
        """
        Tear down a silverberg client
        """
        return self.client._client.disconnect()

    def create_scaling_group(self):
        """
        Creates a scaling group and returns the path.
        """
        def _check_create_body(wrapper):
            self.assertEqual(wrapper.response.code, 201,
                             "Create failed: {0}".format(wrapper.content))
            response = json.loads(wrapper.content)
            for key in request_body:
                self.assertEqual(response["group"][key], request_body[key])
            for key in ("id", "links"):
                self.assertTrue(key in response["group"])

            headers = wrapper.response.headers.getRawHeaders('Location')
            self.assertTrue(headers is not None)
            self.assertEqual(1, len(headers))

            # now make sure the Location header points to something good!
            return _strip_base_url(headers[0])

        request_body = {
            "groupConfiguration": self._config,
            "launchConfiguration": self._launch_server_config,
            "scalingPolicies": self._policies
        }
        deferred = request(
            root, 'POST', '/v1.0/11111/groups', body=json.dumps(request_body))
        deferred.addCallback(_check_create_body)
        return deferred

    # this is not a defer.inlineCallbacks because checking the state and the
    # manifest can be done concurrently
    def create_and_view_scaling_group(self):
        """
        Creating a scaling group with a valid config returns with a 200 OK and
        a Location header pointing to the new scaling group.

        :return: the path to the new scaling group resource
        """

        def _check_manifest_body(wrapper):
            self.assertEqual(wrapper.response.code, 200)

            response = json.loads(wrapper.content)
            self.assertEqual(response["group"]['groupConfiguration'],
                             self._config)
            self.assertEqual(response["group"]['launchConfiguration'],
                             self._launch_server_config)

        def _check_state_body(wrapper):
            self.assertEqual(wrapper.response.code, 200)

            response = json.loads(wrapper.content)
            self.assertTrue(not response["group"]['paused'])
            self.assertTrue(len(response["group"]['pending']),
                            self._config['minEntities'])

        def _check_policies_created(wrapper):
            self.assertEqual(200, wrapper.response.code)
            response = json.loads(wrapper.content)
            self.assertEqual(len(response["policies"]), len(self._policies))

        def _check_creation_worked(path):
            d = defer.gatherResults([
                # request(root, 'GET', path).addCallback(_check_manifest_body),
                # request(root, 'GET', path + '/state').addCallback(
                #     _check_state_body),
                request(root, 'GET', path + '/policies').addCallback(
                    _check_policies_created)
            ])

            # no matter what, just return the path
            return d.addCallback(lambda _: path)

        deferred = self.create_scaling_group()
        deferred.addCallback(_check_creation_worked)
        return deferred

    @defer.inlineCallbacks
    def delete_and_view_scaling_group(self, path):
        """
        Deleting a scaling group returns with a 204 no content.  The next
        attempt to view the scaling group should return a 404 not found.
        """
        wrapper = yield request(root, 'DELETE', path)
        self.assertEqual(wrapper.response.code, 204,
                         "Delete failed: {0}".format(wrapper.content))
        self.assertEqual(wrapper.content, "")

        # now try to view state, manifest, and policies
        # wrapper = yield request(root, 'GET', path)
        # self.assertEqual(wrapper.response.code, 404)
        # wrapper = yield request(root, 'GET', path + '/state')
        # self.assertEqual(wrapper.response.code, 404)
        wrapper = yield request(root, 'GET', path + '/policies')
        self.assertEqual(wrapper.response.code, 404)

        # flush any logged errors
        self.flushLoggedErrors(NoSuchScalingGroupError)

    @defer.inlineCallbacks
    def assert_number_of_scaling_groups(self, number):
        """
        Asserts that there are ``number`` number of scaling groups
        """
        wrapper = yield request(root, 'GET', '/v1.0/11111/groups')
        self.assertEqual(200, wrapper.response.code)
        response = json.loads(wrapper.content)
        self.assertEqual(len(response["groups"]), number)

    @defer.inlineCallbacks
    def test_crud_scaling_group(self):
        """
        Start with no scaling groups.  Create one, make sure it's listed, then
        delete it and make sure it's no longer listed.
        """
        # start with no scaling groups
        yield self.assert_number_of_scaling_groups(0)
        path = yield self.create_and_view_scaling_group()

        # there should now be one scaling group
        yield self.assert_number_of_scaling_groups(1)
        yield self.delete_and_view_scaling_group(path)

        # there should be no scaling groups now
        yield self.assert_number_of_scaling_groups(0)

    @defer.inlineCallbacks
    def test_update_scaling_config(self):
        """
        Editing the config of a scaling group with a valid config returns with
        a 204 no content.  The next attempt to view the scaling config should
        return the new config.  The steady state numbers get updated as well,
        if necessary.
        """
        path = yield self.create_scaling_group()
        config_path = path + '/config'
        edited_config = {
            'name': 'updated_config',
            'cooldown': 5,
            'minEntities': self._config['minEntities'] + 5,
            'maxEntities': (self._config['maxEntities'] or 10) + 5,
            'metadata': {
                'anotherkey': 'anothervalue'
            }
        }

        wrapper = yield request(root, 'PUT', config_path,
                                body=json.dumps(edited_config))
        self.assertEqual(wrapper.response.code, 204,
                         "Edit failed: {0}".format(wrapper.content))
        self.assertEqual(wrapper.content, "")

        # now try to view again - the config should be the edited config
        wrapper = yield request(root, 'GET', config_path)
        self.assertEqual(wrapper.response.code, 200)
        self.assertEqual(json.loads(wrapper.content),
                         {'groupConfiguration': edited_config})

        # make sure the created group has updated pending entities, and is
        # still not paused
        # wrapper = yield request(root, 'GET', path + '/state')
        # self.assertEqual(wrapper.response.code, 200)

        # response = json.loads(wrapper.content)
        # self.assertTrue(not response['group']['paused'])
        # self.assertTrue(len(response['group']['pending']),
        #                 config()[1]['minEntities'] + 5)

    @defer.inlineCallbacks
    def test_update_launch_config(self):
        """
        Editing the launch config of a scaling group with a valid launch config
        returns with a 204 no content.  The next attempt to view the launch
        config should return the new launch config.
        """
        path = yield self.create_scaling_group()
        launch_path = path + '/launch'
        edited_launch = launch_server_config()[1]

        wrapper = yield request(root, 'PUT',
                                launch_path, body=json.dumps(edited_launch))
        self.assertEqual(wrapper.response.code, 204,
                         "Edit failed: {0}".format(wrapper.content))
        self.assertEqual(wrapper.content, "")

        # now try to view again - the config should be the edited config
        wrapper = yield request(root, 'GET', launch_path)
        self.assertEqual(wrapper.response.code, 200)
        self.assertEqual(json.loads(wrapper.content),
                         {'launchConfiguration': edited_launch})


class CassStoreRestScalingPolicyTestCase(TestCase):
    """
    Test case for testing the REST API for the scaling policy specific endpoints
    (but not webhooks) against the mock model.

    As above, this could be made a base case instead... yadda yadda.
    """
    tenant_id = '11111'
    client = None

    def setUp(self):
        """
        Replace the store every time with a clean one.
        """
        if self.client is not None:
            # only do this once
            self.client = client.CQLClient(
                endpoints.clientFromString(reactor, "tcp:ubuntu:9160"),
                "cassresttest")
            store = CassScalingGroupCollection(self.client)
            set_store(store)

            self._config = config()[0]
            self._launch = launch_server_config()[0]

            def _set_group_id(group_id):
                self.group_id = group_id
                self.policies_url = (
                    '/v1.0/{tenant}/groups/{group}/policies'.format(
                        tenant=self.tenant_id, group=self.group_id))

            d = store.create_scaling_group(self.tenant_id, self._config,
                                           self.launch)
            d.addCallback(_set_group_id)
            return d

    def assert_number_of_scaling_policies(self, number):
        """
        Asserts that there are ``number`` number of scaling policies
        """
        wrapper = self.assert_deferred_succeeded(
            request(root, 'GET', self.policies_url))
        self.assertEqual(200, wrapper.response.code)

        response = json.loads(wrapper.content)
        self.assertEqual(len(response["policies"]), number)

    def create_and_view_scaling_policies(self):
        """
        Creating valid scaling policies returns with a 200 OK, a Location
        header pointing to the list of all scaling policies, and a response
        containing a list of the newly created scaling policy resources only.

        :return: a list self links to the new scaling policies (not guaranteed
            to be in any consistent order)
        """
        request_body = policy()[:-1]  # however many of them there are minus one
        wrapper = self.assert_deferred_succeeded(request(
            root, 'POST', self.policies_url, body=json.dumps(request_body)))

        self.assertEqual(wrapper.response.code, 201,
                         "Create failed: {0}".format(wrapper.content))
        response = json.loads(wrapper.content)

        self.assertEqual(len(request_body), len(response["policies"]))

        # this iterates over the response policies, checks to see that each have
        # 'id' and 'links' keys, and then checks to see that the rest of the
        # response policy is in the original set of policies to be created
        for pol in response["policies"]:
            original_pol = pol.copy()
            for key in ('id', 'links'):
                self.assertIn(key, pol)
                del original_pol[key]
            self.assertIn(original_pol, request_body)

        headers = wrapper.response.headers.getRawHeaders('Location')
        self.assertTrue(headers is not None)
        self.assertEqual(1, len(headers))

        # now make sure the Location header points to the list policies header
        self.assertEqual(_strip_base_url(headers[0]), self.policies_url)

        links = [_strip_base_url(link["href"])
                 for link in pol["links"] if link["rel"] == "self"
                 for pol in response["policies"]]
        return links

    def update_and_view_scaling_policy(self, path):
        """
        Updating a scaling policy returns with a 204 no content.  When viewing
        the policy again, it should contain the updated version.
        """
        request_body = policy()[-1]  # the one that was not created
        wrapper = self.assert_deferred_succeeded(
            request(root, 'PUT', path, body=json.dumps(request_body)))
        self.assertEqual(wrapper.response.code, 204,
                         "Update failed: {0}".format(wrapper.content))
        self.assertEqual(wrapper.content, "")

        # now try to view
        wrapper = self.assert_deferred_succeeded(request(root, 'GET', path))
        self.assertEqual(wrapper.response.code, 200)

        response = json.loads(wrapper.content)
        updated = response['policy']

        self.assertIn('id', updated)
        self.assertIn('links', updated)
        self.assertIn(
            path, [_strip_base_url(link["href"]) for link in updated["links"]])

        del updated['id']
        del updated['links']

        self.assertEqual(updated, request_body)

    def delete_and_view_scaling_policy(self, path):
        """
        Deleting a scaling policy returns with a 204 no content.  The next
        attempt to view the scaling policy should return a 404 not found.
        """
        wrapper = self.assert_deferred_succeeded(request(root, 'DELETE', path))
        self.assertEqual(wrapper.response.code, 204,
                         "Delete failed: {0}".format(wrapper.content))
        self.assertEqual(wrapper.content, "")

        # now try to view
        wrapper = self.assert_deferred_succeeded(request(root, 'GET', path))
        self.assertEqual(wrapper.response.code, 404)

        # flush any logged errors
        self.flushLoggedErrors(NoSuchPolicyError)

    def test_crud_scaling_policies(self):
        """
        Start with no policies.  Create some, make sure they're listed,
        create some more because we want to verify that creation response
        contains only the ones that were created.  Then update one of them,
        check changes. Then delete one of them and make sure it's no longer
        listed.
        """
        # start with no scaling groups
        self.assert_number_of_scaling_policies(0)
        first_policies = self.create_and_view_scaling_policies()

        # create more scaling policies, to check the creation response
        self.assert_number_of_scaling_policies(len(first_policies))
        second_policies = self.create_and_view_scaling_policies()
        len_total_policies = len(first_policies) + len(second_policies)
        self.assert_number_of_scaling_policies(len_total_policies)

        # update scaling policy, and there should still be the same number of
        # policies after the update
        self.update_and_view_scaling_policy(first_policies[0])
        self.assert_number_of_scaling_policies(len_total_policies)

        # delete a scaling policy - there should be one fewer scaling policy
        self.delete_and_view_scaling_policy(second_policies[0])
        self.assert_number_of_scaling_policies(len_total_policies - 1)

    test_crud_scaling_policies.skip = True
