import os

from celery.utils.log import get_task_logger
from flask import Blueprint, jsonify

from openedx_webhooks import celery, log_level

# Set up Celery logging.
logger = get_task_logger(__name__)
logger.setLevel(log_level)

def dump_logging_tree():
    """Output a debug tree of the logging system."""
    # pylint: disable=import-outside-toplevel
    import binascii
    import gzip
    import logging_tree
    logging_debug = logging_tree.format.build_description()
    data = binascii.b2a_base64(gzip.compress(logging_debug.encode("utf8"))).decode("utf8")
    print(
        "logging_tree output: " +
        "import binascii,gzip;" +
        f"print(gzip.decompress(binascii.a2b_base64({data!r})).decode('utf8'))"
    )

# dump_logging_tree()

# create a Flask blueprint for getting task status info
tasks = Blueprint('tasks', __name__)

@tasks.route('/status/<task_id>')
def status(task_id):
    result = celery.AsyncResult(task_id)
    return jsonify({
        "status": result.state,
        "info": result.info,
    })

@tasks.route('/status/group:<group_id>')
def group_status(group_id):
    # NOTE: This will only work if the GroupResult
    # has previously called .save() on itself
    group_result = celery.GroupResult.restore(group_id)
    completed_task_ids = []
    failed_task_ids = []
    pending_task_ids = []
    for result in group_result.results:
        if result.successful():
            completed_task_ids.append(result.id)
        elif result.failed():
            failed_task_ids.append(result.id)
        else:
            pending_task_ids.append(result.id)
    return jsonify({
        "task_count": len(group_result.results),
        "completed_task_count": len(completed_task_ids),
        "completed_task_ids": completed_task_ids,
        "failed_task_count": len(failed_task_ids),
        "failed_task_info": failed_task_ids,
        "pending_task_count": len(pending_task_ids),
        "pending_task_info": pending_task_ids,
    })
