"""
Autoscale REST endpoints having to do with editing/modifying the configuration
or launch configuration for a scaling group.

(/tenantId/groups/groupId/config and /tenantId/groups/groupId/launch)
"""
import json

from functools import partial

from otter import controller
from otter.json_schema import group_schemas
from otter.log import log
from otter.log.bound import bound_log_kwargs
from otter.rest.decorators import (
    fails_with,
    succeeds_with,
    validate_body,
    with_transaction_id
)
from otter.rest.errors import InvalidMinEntities, exception_codes
from otter.rest.otterapp import OtterApp
from otter.supervisor import get_supervisor
from otter.util.http import transaction_id


def normalize_launch_config(config):
    """
    Normalize the metadata argument as part of the server arg in the launch
    config - if it is null or invalid, just remove it.
    """
    server_info = config.get('args', {}).get('server', {})

    if server_info.get('metadata') is None:
        server_info.pop('metadata', None)

    return config


class OtterConfig(object):
    """
    REST endpoints for the configuration of scaling groups.
    """
    app = OtterApp()

    def __init__(self, store, tenant_id, group_id, dispatcher):
        self.log = log.bind(system='otter.rest.config',
                            tenant_id=tenant_id,
                            scaling_group_id=group_id)
        self.store = store
        self.tenant_id = tenant_id
        self.group_id = group_id
        self.dispatcher = dispatcher

    @app.route('/', methods=['GET'])
    @with_transaction_id()
    @fails_with(exception_codes)
    @succeeds_with(200)
    def view_config_for_scaling_group(self, request):
        """
        Get the configuration for a scaling group, which includes the minimum
        number of entities, the maximum number of entities, global cooldown,
        and other metadata.  This data is returned in the body of the response
        in JSON format.

        Example response::

            {
                "groupConfiguration": {
                    "name": "workers",
                    "cooldown": 60,
                    "minEntities": 5,
                    "maxEntities": 100,
                    "metadata": {
                        "firstkey": "this is a string",
                        "secondkey": "1",
                    }
                }
            }
        """
        rec = self.store.get_scaling_group(
            self.log, self.tenant_id, self.group_id)
        deferred = rec.view_config()
        deferred.addCallback(
            lambda conf: json.dumps({"groupConfiguration": conf}))
        return deferred

    @app.route('/', methods=['PUT'])
    @with_transaction_id()
    @fails_with(exception_codes)
    @succeeds_with(204)
    @validate_body(group_schemas.update_config)
    def edit_config_for_scaling_group(self, request, data):
        """
        Edit the configuration for a scaling group, which includes the minimum
        number of entities, the maximum number of entities, global cooldown,
        and other metadata.  This data provided in the request body in JSON
        format.  If successful, no response body will be returned.

        Example request::

            {
                "name": "workers",
                "cooldown": 60,
                "minEntities": 5,
                "maxEntities": 100,
                "metadata": {
                    "firstkey": "this is a string",
                    "secondkey": "1",
                }
            }

        The entire schema body must be provided.
        """
        if data['minEntities'] > data['maxEntities']:
            raise InvalidMinEntities(
                "minEntities must be less than or equal to maxEntities")

        def _get_launch_and_obey_config_change(scaling_group, state):
            d = scaling_group.view_launch_config()
            d.addCallback(partial(
                controller.obey_config_change,
                self.log,
                transaction_id(request),
                data, scaling_group, state))
            return d

        group = self.store.get_scaling_group(
            self.log, self.tenant_id, self.group_id)
        deferred = group.update_config(data)
        deferred.addCallback(
            lambda _: controller.modify_and_trigger(
                self.dispatcher,
                group,
                bound_log_kwargs(log),
                _get_launch_and_obey_config_change,
                modify_state_reason='edit_config_for_scaling_group'))
        return deferred


class OtterLaunch(object):
    """
    REST endpoints for launch configurations.
    """
    app = OtterApp()

    def __init__(self, store, tenant_id, group_id):
        self.log = log.bind(system='otter.rest.launch',
                            tenant_id=tenant_id,
                            scaling_group_id=group_id)
        self.store = store
        self.tenant_id = tenant_id
        self.group_id = group_id

    @app.route('/', methods=['GET'])
    @with_transaction_id()
    @fails_with(exception_codes)
    @succeeds_with(200)
    def view_launch_config(self, request):
        """
        Get the launch configuration for a scaling group, which includes the
        details of how to create a server, from what image, which load
        balancers to join it to, and what networks to add it to, and other
        metadata.  This data is returned in the body of the response in JSON
        format.

        Example response::

            {
                "launchConfiguration": {
                    "type": "launch_server",
                    "args": {
                        "server": {
                            "flavorRef": 3,
                            "name": "webhead",
                            "imageRef": "0d589460-f177-4b0f-81c1-8ab8903ac7d8",
                            "OS-DCF:diskConfig": "AUTO",
                            "metadata": {
                                "mykey": "myvalue"
                            },
                            "personality": [
                                {
                                    "path": '/root/.ssh/authorized_keys',
                                    "contents": "ssh-rsa A... user@example.net"
                                }
                            ],
                            "networks": [{
                                "uuid": "11111111-1111-1111-1111-111111111111"
                            }],
                        },
                        "loadBalancers": [
                            {
                                "loadBalancerId": 2200,
                                "port": 8081
                            }
                        ]
                    }
                }
            }
        """
        rec = self.store.get_scaling_group(
            self.log, self.tenant_id, self.group_id)
        deferred = rec.view_launch_config()
        deferred.addCallback(
            lambda conf: json.dumps({"launchConfiguration": conf}))
        return deferred

    @app.route('/', methods=['PUT'])
    @with_transaction_id()
    @fails_with(exception_codes)
    @succeeds_with(204)
    @validate_body(group_schemas.launch_config)
    def edit_launch_config(self, request, data):
        """
        Edit the launch configuration for a scaling group, which includes the
        details of how to create a server, from what image, which load
        balancers to join it to, and what networks to add it to, and other
        metadata.  This data provided in the request body in JSON format.  If
        successful, no response body will be returned.

        Example request::

            {
                "type": "launch_server",
                "args": {
                    "server": {
                        "flavorRef": 3,
                        "name": "webhead",
                        "imageRef": "0d589460-f177-4b0f-81c1-8ab8903ac7d8",
                        "OS-DCF:diskConfig": "AUTO",
                        "metadata": {
                            "mykey": "myvalue"
                        },
                        "personality": [
                            {
                                "path": '/root/.ssh/authorized_keys',
                                "contents": "ssh-rsa A... user@example.net"
                            }
                        ],
                        "networks": [
                            {
                                "uuid": "11111111-1111-1111-1111-111111111111"
                            }
                        ],
                    },
                    "loadBalancers": [
                        {
                            "loadBalancerId": 2200,
                            "port": 8081
                        }
                    ]
                }
            }

        The exact update cases are still up in the air -- can the user provide
        a mimimal schema, and if so, what happens with defaults?

        Nova should validate the image before saving the new config.
        Users may have an invalid configuration based on dependencies.
        """
        rec = self.store.get_scaling_group(
            self.log, self.tenant_id, self.group_id)
        data = normalize_launch_config(data)
        group_schemas.validate_launch_config_servicenet(data)
        deferred = get_supervisor().validate_launch_config(
            self.log, self.tenant_id, data)
        deferred.addCallback(lambda _: rec.update_launch_config(data))
        return deferred
