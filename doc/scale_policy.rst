====================
Scale Policy Schema
====================

**Document type: scale_up_policy**::

    {
        "name": "scale up by one server",
        "change": 1,
        "cooldown": 150,
        "type": "webhook"
    }

**Document type: scale_up_by_percent_policy**::

    {
        "name": "scale up one percent",
        "changePercent": 10,
        "cooldown": 150,
        "type": "webhook"
    }

**Document type: scale_down_policy**::

    {
        "name": "scale down one server",
        "change": -1,
        "cooldown": 150,
        "type": "webhook"
    }

**Document type: scale_down_by_percent_policy**::

    {
        "name": "scale down one percent",
        "changePercent": -10,
        "cooldown": 150,
        "type": "webhook"
    }
**Document type: scale_by_schedule_policy**::

    {
        "name": "scale up by 1 server every hour",
        "change": 1,
        "cooldown": 150,
        "type": "schedule",
        "args": {
            "cron": "0 * * * *"
        }
    }    
