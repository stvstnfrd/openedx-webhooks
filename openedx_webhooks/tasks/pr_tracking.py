"""
State-based updating of the information surrounding pull requests.
"""

from __future__ import annotations

import copy
import dataclasses
import itertools
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple, cast

from glom import glom

from openedx_webhooks.bot_comments import (
    BOT_COMMENT_INDICATORS,
    BOT_COMMENTS_FIRST,
    BotComment,
    extract_data_from_comment,
    format_data_for_comment,
    github_blended_pr_comment,
    github_committer_pr_comment,
    github_committer_merge_ping_comment,
    github_community_pr_comment,
    github_contractor_pr_comment,
)
from openedx_webhooks.info import (
    get_blended_project_id,
    get_bot_comments,
    get_jira_issue_key,
    get_people_file,
    is_bot_pull_request,
    is_committer_pull_request,
    is_contractor_pull_request,
    is_draft_pull_request,
    is_internal_pull_request,
    pull_request_has_cla,
)
from openedx_webhooks.labels import (
    GITHUB_CATEGORY_LABELS,
    GITHUB_STATUS_LABELS,
    JIRA_CATEGORY_LABELS,
)
from openedx_webhooks.lib.github.models import PrId
from openedx_webhooks.oauth import get_github_session, get_jira_session
from openedx_webhooks.tasks import logger
from openedx_webhooks.tasks import github_work
from openedx_webhooks.tasks.jira_work import (
    delete_jira_issue,
    transition_jira_issue,
    update_jira_issue,
)
from openedx_webhooks.types import JiraDict, PrDict
from openedx_webhooks.utils import (
    get_jira_custom_fields,
    get_jira_issue,
    jira_paginated_get,
    log_check_response,
    retry_get,
    sentry_extra_context,
    text_summary,
)


JIRA_EXTRA_FIELDS = [
    "Platform Map Area (Levels 1 & 2)",
    "Platform Map Area (Levels 3 & 4)",
    "Blended Project Status Page",
    "Blended Project ID",
    "Github Lines Added",
    "Github Lines Deleted",
]


@dataclass
class PrCurrentInfo:
    """
    The current information we have for a pull request.
    """
    bot_comments: Set[BotComment] = field(default_factory=set)

    # The text of the first bot comment.
    bot_comment0_text: Optional[str] = None

    # The last-seen state stored in the first bot comment.
    last_seen_state: Dict = field(default_factory=dict)

    # The Jira issue id mentioned on the PR if any.
    jira_mentioned_id: Optional[str] = None

    # The actual Jira issue id.  Can differ from jira__mentioned_id if the
    # issue was moved, or can be None if the issue has been deleted.
    jira_id: Optional[str] = None

    jira_title: Optional[str] = None
    jira_description: Optional[str] = None
    jira_status: Optional[str] = None
    jira_labels: Set[str] = field(default_factory=set)
    jira_epic_key: Optional[str] = None
    jira_epic: Optional[JiraDict] = None
    jira_extra_fields: Dict[str, str] = field(default_factory=dict)

    # The actual set of labels on the pull request.
    github_labels: Set[str] = field(default_factory=set)

    # Did the author make a change that could move us out of "Waiting on
    # Author"?
    author_acted: bool = False


@dataclass
class PrDesiredInfo:
    """
    The information we want to have for a pull request.
    """
    bot_comments: Set[BotComment] = field(default_factory=set)
    jira_project: Optional[str] = None
    jira_title: Optional[str] = None
    jira_description: Optional[str] = None

    # The Jira status to start a new issue at.
    jira_initial_status: Optional[str] = None

    # The Jira status we want to set on an existing issue. Can be None if we
    # don't need to force a new status, but can leave the existing status.
    jira_status: Optional[str] = None

    jira_labels: Set[str] = field(default_factory=set)
    jira_epic: Optional[JiraDict] = None
    jira_extra_fields: Dict[str, str] = field(default_factory=dict)

    # The bot-controlled labels we want to on the pull request.
    # See labels.py:CATEGORY_LABELS
    github_labels: Set[str] = field(default_factory=set)


def existing_bot_comments(prid: PrId) -> Tuple[Optional[str], Set[BotComment]]:
    """
    Get the set of bot comments already on the pull request.

    Returns a tuple:
        comment0: the text of the first (most important) bot comment.
        comment_ids: set of bot comment ids.
    """
    comment0 = None
    comment_ids = set()
    for i, comment in enumerate(get_bot_comments(prid)):
        body = comment["body"]
        if i == 0:
            comment0 = body
        for comment_id, snips in BOT_COMMENT_INDICATORS.items():
            if any(snip in body for snip in snips):
                comment_ids.add(comment_id)
    return comment0, comment_ids


def current_support_state(pr: PrDict) -> PrCurrentInfo:
    """
    Examine the world to determine what the current support state is.
    """
    prid = PrId.from_pr_dict(pr)
    current = PrCurrentInfo()
    current.bot_comment0_text, current.bot_comments = existing_bot_comments(prid)
    if current.bot_comment0_text is not None:
        current.last_seen_state = extract_data_from_comment(current.bot_comment0_text)
    current.jira_id = current.jira_mentioned_id = get_jira_issue_key(prid)
    if current.jira_id:
        issue = get_jira_issue(current.jira_id, missing_ok=True)
        if issue is None:
            # Issue has been deleted. Forget about it, and we'll make a new one.
            current.jira_id = None
        else:
            current.jira_id = issue["key"]
            current.jira_title = issue["fields"]["summary"] or ""
            current.jira_description = issue["fields"]["description"] or ""
            current.jira_status = issue["fields"]["status"]["name"]
            current.jira_labels = set(issue["fields"]["labels"])

            custom_fields = get_jira_custom_fields(get_jira_session())
            current.jira_epic_key = issue["fields"].get(custom_fields["Epic Link"])
            current.jira_extra_fields = {
                name: value
                for name in JIRA_EXTRA_FIELDS
                if (value := issue["fields"][custom_fields[name]]) is not None
            }
    current.github_labels = set(lbl["name"] for lbl in pr["labels"])

    if current.last_seen_state.get("draft", False) and not is_draft_pull_request(pr):
        # It was a draft, but now isn't.  The author acted.
        current.author_acted = True

    return current


def desired_support_state(pr: PrDict) -> Optional[PrDesiredInfo]:
    """
    Examine a pull request to decide what state we want the world to be in.
    """
    user = pr["user"]["login"]
    repo = pr["base"]["repo"]["full_name"]
    num = pr["number"]

    if is_bot_pull_request(pr):
        logger.info(f"@{user} is a bot, ignored.")
        return None

    if is_internal_pull_request(pr):
        logger.info(f"@{user} opened PR {repo}#{num} (internal PR)")
        return None

    desired = PrDesiredInfo()

    if is_contractor_pull_request(pr):
        desired.bot_comments.add(BotComment.CONTRACTOR)
        return desired

    if pr["state"] == "open":
        state = "open"
    elif pr["merged"]:
        state = "merged"
    else:
        state = "closed"

    desired.jira_initial_status = "Needs Triage"
    desired.jira_title = pr["title"]
    desired.jira_description = pr["body"] or ""

    has_signed_agreement = pull_request_has_cla(pr)
    blended_id = get_blended_project_id(pr)
    if blended_id is not None:
        comment = BotComment.BLENDED
        desired.jira_project = "BLENDED"
        desired.github_labels.add("blended")
        desired.jira_labels.add("blended")
        blended_epic = find_blended_epic(blended_id)
        if blended_epic is not None:
            desired.jira_epic = blended_epic
            custom_fields = get_jira_custom_fields(get_jira_session())
            map_1_2 = blended_epic["fields"].get(custom_fields["Platform Map Area (Levels 1 & 2)"])
            if map_1_2 is not None:
                desired.jira_extra_fields["Platform Map Area (Levels 1 & 2)"] = map_1_2
    else:
        comment = BotComment.WELCOME
        desired.jira_project = "OSPR"
        desired.github_labels.add("open-source-contribution")
        committer = is_committer_pull_request(pr)
        if committer:
            comment = BotComment.CORE_COMMITTER
            desired.jira_labels.add("core-committer")
            desired.jira_initial_status = "Waiting on Author"
            desired.bot_comments.add(BotComment.CORE_COMMITTER)
            desired.github_labels.add("core committer")
            if state == "merged":
                desired.bot_comments.add(BotComment.CHAMPION_MERGE_PING)

    desired.bot_comments.add(comment)

    # Some PR states mean we want to insist on a Jira status.
    if is_draft_pull_request(pr):
        desired.jira_initial_status = "Waiting on Author"
        desired.bot_comments.add(BotComment.END_OF_WIP)

    if not has_signed_agreement:
        desired.bot_comments.add(BotComment.NEED_CLA)
        desired.jira_initial_status = "Community Manager Review"
        desired.github_labels.add('NEED-CLA')

    if state == "closed":
        desired.jira_status = "Rejected"
    elif state == "merged":
        desired.jira_status = "Merged"

    if has_signed_agreement:
        desired.bot_comments.add(BotComment.OK_TO_TEST)

    if "additions" in pr:
        desired.jira_extra_fields["Github Lines Added"] = pr["additions"]
    if "deletions" in pr:
        desired.jira_extra_fields["Github Lines Deleted"] = pr["deletions"]

    return desired


def json_safe_dict(dc):
    """
    Make a JSON-safe dict from a dataclass, for recording info during dry runs.
    """
    return {k:repr(v) for k, v in dataclasses.asdict(dc).items()}


class PrTrackingFixer:
    """
    Complex logic to compare the current and desired states and make needed changes.
    """

    def __init__(self, pr: PrDict, current: PrCurrentInfo, desired: PrDesiredInfo, actions: FixingActions = None):
        self.pr = pr
        self.current = current
        self.desired = desired
        self.prid = PrId.from_pr_dict(self.pr)
        self.actions = actions or FixingActions(self.prid)

        self.last_seen_state = copy.deepcopy(current.last_seen_state)
        self.happened = False

    def result(self) -> Tuple[Optional[str], bool]:
        return self.current.jira_id, self.happened

    def fix(self) -> None:
        """
        The main routine for making needed changes.
        """
        self.actions.initial_state(
            current=json_safe_dict(self.current),
            desired=json_safe_dict(self.desired),
        )

        self.actions.synchronize_labels(repo=self.prid.full_name)

        comment_kwargs = {}

        make_issue = False

        # We might have an issue already, but in the wrong project.
        if self.current.jira_id is not None:
            assert self.current.jira_mentioned_id is not None
            mentioned_project = self.current.jira_mentioned_id.partition("-")[0]
            actual_project = self.current.jira_id.partition("-")[0]
            if mentioned_project != self.desired.jira_project:
                if actual_project == self.desired.jira_project:
                    # Looks like the issue already got moved to the right project.
                    pass
                else:
                    # Delete the existing issue and forget the current state.
                    self.actions.delete_jira_issue(jira_id=self.current.jira_id)
                    make_issue = True
                    comment_kwargs["deleted_issue_key"] = self.current.jira_mentioned_id
                    self.current.jira_id = None
                    self.current.jira_title = None
                    self.current.jira_description = None
                    self.current.jira_status = None

        # If we want an issue and none is mentioned yet, we need to make one.
        if self.desired.jira_project is not None:
            if self.current.jira_mentioned_id is None:
                make_issue = True

        # If needed, make a Jira issue.
        if make_issue:
            self._make_jira_issue()
        else:
            # Epics are a bit odd: sometimes we have the full issue, sometimes
            # just the key. Make sure the full issue is available where we need
            # it.
            if self.desired.jira_epic is not None:
                if self.current.jira_epic_key == self.desired.jira_epic["key"]:
                    self.current.jira_epic = self.desired.jira_epic

        # Draftiness
        self.last_seen_state["draft"] = is_draft_pull_request(self.pr)

        if self.current.jira_id:
            # If the author acted, and we were waiting on the author, then we
            # should set the status to this PR's usual initial status.
            if self.current.author_acted and self.current.jira_status == "Waiting on Author":
                self.desired.jira_status = self.desired.jira_initial_status

            # Check the state of the Jira issue.
            if self.desired.jira_status is not None and self.desired.jira_status != self.current.jira_status:
                self.actions.transition_jira_issue(jira_id=self.current.jira_id, jira_status=self.desired.jira_status)
                self.current.jira_status = self.desired.jira_status
                self.happened = True

            # Update the Jira issue information.
            self._fix_jira_information()

        # Check the GitHub labels.
        self._fix_github_labels()

        # Check the bot comments.
        fix_comment = True
        if self.pr["state"] == "closed" and self.current.bot_comments:
            # If the PR is closed and already has bot comments, then don't
            # change the bot comment.
            fix_comment = False
        if fix_comment:
            self._fix_bot_comment(comment_kwargs)
        self._add_bot_comments()

    def _make_jira_issue(self) -> None:
        """
        Make our desired Jira issue.
        """
        assert self.desired.jira_project is not None
        extra_fields = self.desired.jira_extra_fields
        if self.desired.jira_epic:
            extra_fields["Epic Link"] = self.desired.jira_epic["key"]
        user_name, institution = get_name_and_institution_for_pr(self.pr)
        new_issue = self.actions.create_ospr_issue(
            pr_url=self.pr["html_url"],
            project=self.desired.jira_project,
            summary=self.desired.jira_title,
            description=self.desired.jira_description,
            labels=list(self.desired.jira_labels),
            user_name=user_name,
            institution=institution,
            extra_fields=extra_fields,
        )
        self.current.jira_id = new_issue["key"]
        assert self.current.jira_id is not None
        self.current.jira_status = new_issue["fields"]["status"]["name"]
        self.current.jira_title = self.desired.jira_title
        self.current.jira_description = self.desired.jira_description
        self.current.jira_labels = self.desired.jira_labels
        self.current.jira_epic = self.desired.jira_epic
        if self.current.jira_epic is not None:
            self.current.jira_epic_key = self.current.jira_epic["key"]

        if self.desired.jira_initial_status != self.current.jira_status:
            assert self.desired.jira_initial_status is not None
            self.actions.transition_jira_issue(
                jira_id=self.current.jira_id,
                jira_status=self.desired.jira_initial_status,
            )
            self.current.jira_status = self.desired.jira_initial_status

        self.happened = True

    def _fix_jira_information(self) -> None:
        """
        Update the information on the Jira issue.
        """
        update_kwargs: Dict[str, Any] = {}

        if self.desired.jira_title != self.current.jira_title:
            update_kwargs["summary"] = self.desired.jira_title
        if self.desired.jira_description != self.current.jira_description:
            update_kwargs["description"] = self.desired.jira_description

        desired_labels = set(self.desired.jira_labels)
        ad_hoc_labels = self.current.jira_labels - JIRA_CATEGORY_LABELS
        desired_labels.update(ad_hoc_labels)
        if desired_labels != self.current.jira_labels:
            update_kwargs["labels"] = list(self.desired.jira_labels)

        if self.desired.jira_epic is not None:
            if self.current.jira_epic is None or (self.desired.jira_epic["key"] != self.current.jira_epic["key"]):
                update_kwargs["epic_link"] = self.desired.jira_epic["key"]

        # Only update extra fields if the fields we want have changed.
        current_extra_fields = {
            k: v for k, v in self.current.jira_extra_fields.items()
            if k in self.desired.jira_extra_fields
        }
        if self.desired.jira_extra_fields != current_extra_fields:
            update_kwargs["extra_fields"] = self.desired.jira_extra_fields

        if update_kwargs:
            assert self.current.jira_id is not None
            try:
                self.actions.update_jira_issue(jira_id=self.current.jira_id, **update_kwargs)
            except:
                logger.warning(f"Couldn't update jira: {update_kwargs=}, {self.current.jira_description=}, {current_extra_fields=}")
                raise
            self.current.jira_title = self.desired.jira_title
            self.current.jira_description = self.desired.jira_description
            self.current.jira_labels = self.desired.jira_labels
            self.current.jira_epic = self.desired.jira_epic
            self.current.jira_extra_fields = self.desired.jira_extra_fields
            self.happened = True

    def _fix_github_labels(self) -> None:
        """
        Reconcile the desired bot labels with the actual labels on GitHub.
        Take care to preserve any label we've never heard of.
        """
        desired_labels = set(self.desired.github_labels)
        if self.current.jira_status:
            desired_labels.add(self.current.jira_status.lower())
        ad_hoc_labels = self.current.github_labels - GITHUB_CATEGORY_LABELS - GITHUB_STATUS_LABELS
        desired_labels.update(ad_hoc_labels)

        if desired_labels != self.current.github_labels:
            self.actions.update_labels_on_pull_request(
                labels=list(desired_labels),
            )
            self.happened = True

    def _fix_bot_comment(self, comment_kwargs: Dict) -> None:
        """
        Reconcile the desired comments from the bot with what the bot has said.

        This only updates the first bot comment.
        """
        has_bot_comments = bool(self.current.bot_comments)

        # The comments we need in the first bot comment. Because we reconstruct
        # the full comment, we don't exclude the existing bot comments from the
        # set.
        needed_comments = self.desired.bot_comments & BOT_COMMENTS_FIRST

        comment_body = ""

        # The issue could have been deleted, we'll continue to talk about the
        # now-gone issue. it's better than nothing.
        jira_id = cast(str, self.current.jira_id or self.current.jira_mentioned_id)
        if BotComment.WELCOME in needed_comments:
            comment_body += github_community_pr_comment(self.pr, jira_id, **comment_kwargs)
            needed_comments.remove(BotComment.WELCOME)

        if BotComment.CONTRACTOR in needed_comments:
            comment_body += github_contractor_pr_comment(self.pr, **comment_kwargs)
            needed_comments.remove(BotComment.CONTRACTOR)

        if BotComment.CORE_COMMITTER in needed_comments:
            comment_body += github_committer_pr_comment(self.pr, jira_id, **comment_kwargs)
            needed_comments.remove(BotComment.CORE_COMMITTER)

        if BotComment.BLENDED in needed_comments:
            comment_body += github_blended_pr_comment(
                self.pr,
                jira_id,
                self.current.jira_epic,
                **comment_kwargs
            )
            needed_comments.remove(BotComment.BLENDED)

        if BotComment.OK_TO_TEST in needed_comments:
            if comment_body:
                comment_body += "\n<!-- jenkins ok to test -->"
            needed_comments.remove(BotComment.OK_TO_TEST)

        # These are handled in github_community_pr_comment and github_blended_pr_comment.
        if BotComment.NEED_CLA in needed_comments:
            needed_comments.remove(BotComment.NEED_CLA)
        if BotComment.END_OF_WIP in needed_comments:
            needed_comments.remove(BotComment.END_OF_WIP)
        if BotComment.WELCOME_CLOSED in needed_comments:
            needed_comments.remove(BotComment.WELCOME_CLOSED)

        comment_body += format_data_for_comment(self.last_seen_state)

        if comment_body != self.current.bot_comment0_text:
            # If there are current-state comments, then we need to edit the
            # comment, otherwise create one.
            if has_bot_comments:
                self.actions.edit_comment_on_pull_request(comment_body=comment_body)
            else:
                self.actions.add_comment_to_pull_request(comment_body=comment_body)
            self.happened = True

        assert needed_comments == set(), f"Couldn't make first comments: {needed_comments}"

    def _add_bot_comments(self):
        """
        Add any additional bot comments as needed.

        More comments can be added as subsequent comments. We need anything
        in desired.bot_comments that we don't already have and that aren't
        first-comment parts.
        """
        needed_comments = self.desired.bot_comments - self.current.bot_comments - BOT_COMMENTS_FIRST

        if BotComment.CHAMPION_MERGE_PING in needed_comments:
            champions = get_champions_for_pr(self.pr)
            body = github_committer_merge_ping_comment(self.pr, champions)
            self.actions.add_comment_to_pull_request(comment_body=body)
            needed_comments.remove(BotComment.CHAMPION_MERGE_PING)

        assert needed_comments == set(), f"Couldn't make comments: {needed_comments}"


def find_blended_epic(project_id: int) -> Optional[JiraDict]:
    """
    Find the blended epic for a blended project.
    """
    jql = (
        '(' +
        '"Blended Project ID" ~ "BD-00{id}" OR ' +
        '"Blended Project ID" ~ "BD-0{id}" OR ' +
        '"Blended Project ID" ~ "BD-{id}"' +
        ')' +
        ' AND project = Blended AND type = Epic'
    ).format(id=project_id)
    issues = list(jira_paginated_get("/rest/api/2/search", jql=jql, obj_name="issues", session=get_jira_session()))
    issue = None
    if not issues:
        logger.info(f"Couldn't find a blended epic for {project_id}")
    elif len(issues) > 1:
        keys = [iss["key"] for iss in issues]
        logger.error(f"Found more than one blended epic for {project_id}: {keys}")
    else:
        issue = issues[0]
    return issue


def get_name_and_institution_for_pr(pr: PrDict) -> Tuple[str, Optional[str]]:
    """
    Get the author name and institution for a pull request.

    The returned name will always be a string. The institution might be None.

    Returns:
        name, institution
    """
    user = pr["user"]["login"]
    people = get_people_file()

    user_name = None
    if user in people:
        user_name = people[user].get("name", "")
    if not user_name:
        resp = retry_get(get_github_session(), pr["user"]["url"])
        if resp.ok:
            user_name = resp.json().get("name", user)
        else:
            user_name = user

    institution = people.get(user, {}).get("institution", None)

    return user_name, institution


def get_champions_for_pr(pr: PrDict) -> List[str]:
    """
    Get a list of GitHub nicks for the edX champions for this core committer
    pull request.
    """
    user = pr["user"]["login"]
    people = get_people_file()
    user_data = people.get(user, {})
    return glom(user_data, "committer.champions", default=[])


class DryRunFixingActions:
    """
    Implementation of actions for dry runs.
    """
    jira_ids = itertools.count(start=9000)

    def __init__(self):
        self.action_calls = []

    def create_ospr_issue(self, **kwargs):
        # This needs a special override because it has to return a Jira key.
        self.action_calls.append(("create_ospr_issue", kwargs))
        return {
            "key": f"OSPR-{next(self.jira_ids)}",
            "fields": {
                "status": {
                    "name": "Needs Triage",
                },
            },
        }

    def __getattr__(self, name):
        def fn(**kwargs):
            self.action_calls.append((name, kwargs))
        return fn


class FixingActions:
    """
    Implementation for actions needed by the pull request fixer.

    These actions actually make the changes needed. All arguments
    must be JSON-serializable so that dry-runs can report on the
    actions.

    """

    def __init__(self, prid: PrId):
        self.prid = prid

    def initial_state(self, *, current: Dict, desired: Dict) -> None:
        """
        Does nothing when really fixing, but captures information for dry runs.
        """

    def synchronize_labels(self, *, repo: str) -> None:
        github_work.synchronize_labels(repo)

    def create_ospr_issue(
        self, *,
        pr_url: str,
        project: str,
        summary: Optional[str],
        description: Optional[str],
        labels: List[str],
        user_name: Optional[str],
        institution: Optional[str],
        extra_fields: Dict[str, str],
    ) -> Dict:
        """
        Create a new OSPR or OSPR-like issue for a pull request.

        Returns the JSON describing the issue.
        """

        custom_fields = get_jira_custom_fields(get_jira_session())
        new_issue = {
            "fields": {
                "project": {
                    "key": project,
                },
                "issuetype": {
                    "name": "Pull Request Review",
                },
                "summary": summary,
                "description": description,
                "labels": labels,
                "customfield_10904": pr_url,            # "URL" is ambiguous, use the internal name.
                custom_fields["PR Number"]: self.prid.number,
                custom_fields["Repo"]: self.prid.full_name,
                custom_fields["Contributor Name"]: user_name,
            }
        }
        if institution:
            new_issue["fields"][custom_fields["Customer"]] = [institution]
        if extra_fields:
            for name, value in extra_fields.items():
                new_issue["fields"][custom_fields[name]] = value
        sentry_extra_context({"new_issue": new_issue})

        logger.info(f"Creating new JIRA issue for PR {self.prid}...")
        resp = get_jira_session().post("/rest/api/2/issue", json=new_issue)
        log_check_response(resp)

        # Jira only sends the key.  Put it into the JSON we started with, and
        # return it as the state of the issue.
        new_issue_body = resp.json()
        new_issue["key"] = new_issue_body["key"]
        # Our issues all start as "Needs Triage".
        new_issue["fields"]["status"] = {"name": "Needs Triage"}
        return new_issue

    def delete_jira_issue(self, *, jira_id: str) -> None:
        delete_jira_issue(jira_id)

    def transition_jira_issue(self, *, jira_id: str, jira_status: str) -> None:
        transition_jira_issue(jira_id, jira_status)

    def update_jira_issue(self, *, jira_id: str, **update_kwargs) -> None:
        update_jira_issue(jira_id, **update_kwargs)

    def add_comment_to_pull_request(self, *, comment_body: str) -> None:
        """
        Add a comment to a pull request.
        """
        url = f"/repos/{self.prid.full_name}/issues/{self.prid.number}/comments"
        logger.info(f"Commenting on PR {self.prid}: {text_summary(comment_body, 90)!r}")
        resp = get_github_session().post(url, json={"body": comment_body})
        log_check_response(resp)

    def edit_comment_on_pull_request(self, *, comment_body: str) -> None:
        """
        Edit the bot-authored comment on this pull request.
        """
        bot_comments = list(get_bot_comments(self.prid))
        comment_id = bot_comments[0]["id"]
        url = f"/repos/{self.prid.full_name}/issues/comments/{comment_id}"
        logger.info(f"Updating comment on PR {self.prid}: {text_summary(comment_body, 90)!r}")
        resp = get_github_session().patch(url, json={"body": comment_body})
        log_check_response(resp)

    def update_labels_on_pull_request(self, *, labels: List[str]) -> None:
        """
        Change the labels on a pull request.

        Arguments:
            labels: a list of strings.
        """
        url = f"/repos/{self.prid.full_name}/issues/{self.prid.number}"
        logger.info(f"Patching labels on PR {self.prid}: {labels}")
        resp = get_github_session().patch(url, json={"labels": labels})
        log_check_response(resp)
