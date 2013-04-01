"""
JSON schema to be used to verify the return values from implementations of the
model interface.

This is only going to be used internally to verify the schemas returned by the
interface.  Probably easier to test just by asserting simplified dictionaries
so that all the correct data doesn't have to be mocked.

Please delete from this file.
"""
from copy import deepcopy
from otter.json_schema import group_schemas


# unlike updating or inputing a group config, the returned config must actually
# have all the properties
group_config = deepcopy(group_schemas.config)
for property_name in group_config['properties']:
    group_config['properties'][property_name]['required'] = True


# view manifest returns a dictionary with keys being the ids
view_manifest = {
    "type": "object",
    "description": "Schema returned by the interface for viewing a manifest",
    "properties": {
        "groupConfiguration": group_schemas.config,
        "launchConfiguration": group_schemas.launch_config,
        "scalingPolicies": {
            "type": "object",
            "patternProperties": {
                "^\S+$": {
                    "type": "object",
                    "required": True,
                    "items": [group_schemas.policy]
                }
            },
            "required": True,
            "additionalProperties": False
        }
    },
    "additionalProperties": False
}


# example:
# {
#     "f236a93f-a46d-455c-9403-f26838011522": {
#         "name": "scale up by 10",
#         "change": 10,
#         "cooldown": 5
#     },
#     "e27040e5-527e-4710-b8a9-98e5e9aff2f0": {
#         "name": "scale down a 5.5 percent because of a tweet",
#         "changePercent": -5.5,
#         "cooldown": 6
#     },
#     "228dbf91-7b15-4d21-8de2-fa584f01a440": {
#         "name": "set number of servers to 10",
#         "steadyState": 10,
#         "cooldown": 3
#     }
# }
policy_list = {
    "type": "object",
    "patternProperties": {
        "^\S+$": {
            "type": "object",
            "required": True,
            "items": [group_schemas.policy]
        }
    },
    "required": False,
    "additionalProperties": False
}

webhook = deepcopy(group_schemas.webhook)
webhook['properties']['metadata']['required'] = True
webhook['properties']['capability'] = {
    "type": "object",
    "properties": {
        "hash": {
            "type": "string",
            "description": 'The "unguessable" part of the capability URL',
            "required": True,
        },
        "version": {
            "type": "string",
            "description": ("The version of capability generation used to make "
                            "the capabilityHash"),
            "required": True,
            "pattern": "\S+"
        }
    },
    "additionalProperties": False,
    "required": True
}
webhook['required'] = True


webhook_list = {
    "type": "object",
    "description": "Schema returned by the interface for viewing all webhooks",
    "patternProperties": {"^\S+$": webhook},
    "additionalProperties": False
}