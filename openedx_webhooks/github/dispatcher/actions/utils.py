"""
Utilities for GitHub webhook handler actions.
"""
from typing import Optional

from openedx_webhooks.info import pull_request_has_cla
from openedx_webhooks.oauth import get_github_session
from openedx_webhooks.tasks import logger
from openedx_webhooks.types import PrDict
from openedx_webhooks.utils import log_check_response


def find_issues_for_pull_request(jira, pull_request_url):
    """
    Find corresponding JIRA issues for a given GitHub pull request.

    Arguments:
        jira (jira.JIRA): An authenticated JIRA API client session
        pull_request_url (str)

    Returns:
        jira.client.ResultList[jira.Issue]
    """
    jql = 'project=OSPR AND cf[10904]="{}"'.format(pull_request_url)
    return jira.search_issues(jql)


def _get_latest_commit_for_pull_request(repo_name_full: str, number: int) -> str:
    """
    Lookup PR commit details and pull out the SHA of the most recent commit
    """
    data = _get_latest_commit_for_pull_request_data(repo_name_full, number)
    commit = {}
    if data:
        commit = data[-1]
    sha = commit.get('sha')
    logger.debug("CLA: SHA %s", sha, repo_name_full)
    return sha


def _get_latest_commit_for_pull_request_data(repo_name_full: str, number: int) -> str:
    """
    Lookup the HEAD commit SHA for a pull request
    """
    url = f"https://api.github.com/repos/{repo_name_full}/pulls/{number}/commits"
    logger.debug("CLA: GET %s", url)
    response = get_github_session().get(url)
    log_check_response(response)
    data = response.json()
    logger.debug("CLA: GOT %s", data)
    return data


def _get_commit_status_for_cla(url):
    """
    Send a GET request to the Github API to lookup the build status
    """
    logger.debug("CLA: GET %s", url)
    response = get_github_session().get(url)
    log_check_response(response)
    data = response.json()
    logger.debug("CLA: GOT %s %s", url, data)
    cla_status = [
        status
        for status in data
        if status.get('context') == 'cla'
    ]
    state = None
    if len(cla_status) > 0:
        state = cla_status[-1].get('state')
    return state


def _update_commit_status_for_cla(url, payload):
    """
    Send a POST request to the Github API to update the build status
    """
    logger.debug("CLA: POST %s %s", url, payload)
    response = get_github_session().post(url, json=payload)
    log_check_response(response)
    data = response.json()
    logger.debug("CLA: PAST %s %s", url, data)
    return data


def update_commit_status_for_cla(pull_request: PrDict) -> Optional[bool]:
    """
    Set the CLA build status (success or failure)
    """
    repo_name_full = pull_request['base']['repo']['full_name']
    number = pull_request['number']
    sha = _get_latest_commit_for_pull_request(repo_name_full, number)
    if not sha:
        return None
    has_signed_agreement = pull_request_has_cla(pull_request)
    status = 'failure'
    if has_signed_agreement:
        status = 'success'
    url = f"https://api.github.com/repos/{repo_name_full}/statuses/{sha}"
    state = _get_commit_status_for_cla(url)
    if state != status:
        payload = {
            'context': 'cla',
            'description': 'We need a signed CLA',
            'state': status,
            # pylint: disable=line-too-long
            'target_url': 'https://openedx.atlassian.net/wiki/spaces/COMM/pages/941457737/How+to+start+contributing+to+the+Open+edX+code+base',
            # pylint: enable=line-too-long
        }
        data = _update_commit_status_for_cla(url, payload)
        if data is None:
            return None
    if status == 'success':
        return True
    return False
