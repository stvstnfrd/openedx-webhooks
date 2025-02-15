"""Tests of tasks/github.py:pull_request_changed for opening pull requests."""

import itertools
from datetime import datetime

import pytest

from openedx_webhooks.bot_comments import (
    BotComment,
    is_comment_kind,
)
from openedx_webhooks.info import get_jira_issue_key
from openedx_webhooks.tasks.github import pull_request_changed


# These tests should run when we want to test flaky GitHub behavior.
pytestmark = pytest.mark.flaky_github


@pytest.fixture
def sync_labels_fn(mocker):
    """A patch on synchronize_labels"""
    return mocker.patch("openedx_webhooks.tasks.github_work.synchronize_labels")


def test_internal_pr_opened(reqctx, fake_github):
    pr = fake_github.make_pull_request(user="nedbat")
    with reqctx:
        key, anything_happened = pull_request_changed(pr.as_json())
    assert key is None
    assert anything_happened is False
    assert len(pr.list_comments()) == 0


def test_pr_opened_by_bot(reqctx, fake_github):
    fake_github.make_user(login="some_bot", type="Bot")
    pr = fake_github.make_pull_request(user="some_bot")
    with reqctx:
        key, anything_happened = pull_request_changed(pr.as_json())
    assert key is None
    assert anything_happened is False
    assert len(pr.list_comments()) == 0


def test_external_pr_opened_no_cla(reqctx, sync_labels_fn, fake_github, fake_jira):
    # No CLA, because this person is not in people.yaml
    fake_github.make_user(login="new_contributor", name="Newb Contributor")
    pr = fake_github.make_pull_request(owner="edx", repo="edx-platform", user="new_contributor")
    prj = pr.as_json()

    with reqctx:
        issue_id, anything_happened = pull_request_changed(prj)

    assert issue_id is not None
    assert issue_id.startswith("OSPR-")
    assert anything_happened is True

    # Check the Jira issue that was created.
    assert len(fake_jira.issues) == 1
    issue = fake_jira.issues[issue_id]
    assert issue.contributor_name == "Newb Contributor"
    assert issue.customer is None
    assert issue.pr_number == prj["number"]
    assert issue.repo == prj["base"]["repo"]["full_name"]
    assert issue.url == prj["html_url"]
    assert issue.description == prj["body"]
    assert issue.issuetype == "Pull Request Review"
    assert issue.summary == prj["title"]
    assert issue.labels == set()

    # Check that the Jira issue was moved to Community Manager Review.
    assert issue.status == "Community Manager Review"

    # Check that we synchronized labels.
    sync_labels_fn.assert_called_once_with("edx/edx-platform")

    # Check the GitHub comment that was created.
    pr_comments = pr.list_comments()
    assert len(pr_comments) == 1
    body = pr_comments[0].body
    jira_link = "[{id}](https://openedx.atlassian.net/browse/{id})".format(id=issue_id)
    assert jira_link in body
    assert "Thanks for the pull request, @new_contributor!" in body
    assert is_comment_kind(BotComment.NEED_CLA, body)
    assert is_comment_kind(BotComment.WELCOME, body)
    assert not is_comment_kind(BotComment.OK_TO_TEST, body)

    # Check the GitHub labels that got applied.
    assert pr.labels == {
        'community manager review',
        'open-source-contribution',
        'NEED-CLA',
    }


def test_external_pr_opened_with_cla(reqctx, sync_labels_fn, fake_github, fake_jira):
    pr = fake_github.make_pull_request(owner="edx", repo="some-code", user="tusbar", number=11235)
    prj = pr.as_json()

    with reqctx:
        issue_id, anything_happened = pull_request_changed(prj)

    assert issue_id is not None
    assert issue_id.startswith("OSPR-")
    assert anything_happened is True

    # Check the Jira issue that was created.
    assert len(fake_jira.issues) == 1
    issue = fake_jira.issues[issue_id]
    assert issue.contributor_name == "Bertrand Marron"
    assert issue.customer == ["IONISx"]
    assert issue.pr_number == 11235
    assert issue.repo == "edx/some-code"
    assert issue.url == prj["html_url"]
    assert issue.description == prj["body"]
    assert issue.issuetype == "Pull Request Review"
    assert issue.summary == prj["title"]
    assert issue.labels == set()

    # Check that the Jira issue is in Needs Triage.
    assert issue.status == "Needs Triage"

    # Check that we synchronized labels.
    sync_labels_fn.assert_called_once_with("edx/some-code")

    # Check the GitHub comment that was created.
    pr_comments = pr.list_comments()
    assert len(pr_comments) == 1
    body = pr_comments[0].body
    jira_link = "[{id}](https://openedx.atlassian.net/browse/{id})".format(id=issue_id)
    assert jira_link in body
    assert "Thanks for the pull request, @tusbar!" in body
    assert is_comment_kind(BotComment.WELCOME, body)
    assert not is_comment_kind(BotComment.NEED_CLA, body)
    assert is_comment_kind(BotComment.OK_TO_TEST, body)

    # Check the GitHub labels that got applied.
    assert pr.labels == {"needs triage", "open-source-contribution"}


def test_core_committer_pr_opened(reqctx, sync_labels_fn, fake_github, fake_jira):
    pr = fake_github.make_pull_request(user="felipemontoya", owner="edx", repo="edx-platform")
    prj = pr.as_json()

    with reqctx:
        issue_id, anything_happened = pull_request_changed(prj)

    assert issue_id is not None
    assert issue_id.startswith("OSPR-")
    assert anything_happened is True

    # Check the Jira issue that was created.
    assert len(fake_jira.issues) == 1
    issue = fake_jira.issues[issue_id]
    assert issue.contributor_name == "Felipe Montoya"
    assert issue.customer == ["EduNEXT"]
    assert issue.pr_number == prj["number"]
    assert issue.repo == prj["base"]["repo"]["full_name"]
    assert issue.url == prj["html_url"]
    assert issue.description == prj["body"]
    assert issue.issuetype == "Pull Request Review"
    assert issue.summary == prj["title"]
    assert issue.labels == {"core-committer"}

    # Check that the Jira issue was moved to Waiting on Author
    assert issue.status == "Waiting on Author"

    # Check that we synchronized labels.
    sync_labels_fn.assert_called_once_with("edx/edx-platform")

    # Check the GitHub comment that was created.
    pr_comments = pr.list_comments()
    assert len(pr_comments) == 1
    body = pr_comments[0].body
    jira_link = "[{id}](https://openedx.atlassian.net/browse/{id})".format(id=issue_id)
    assert jira_link in body
    assert "Thanks for the pull request, @felipemontoya!" in body
    assert is_comment_kind(BotComment.CORE_COMMITTER, body)
    assert not is_comment_kind(BotComment.NEED_CLA, body)
    assert is_comment_kind(BotComment.OK_TO_TEST, body)

    # Check the GitHub labels that got applied.
    assert pr.labels == {"waiting on author", "open-source-contribution", "core committer"}


def test_old_core_committer_pr_opened(reqctx, sync_labels_fn, fake_github, fake_jira):
    # No-one was a core committer before June 2020.
    # This test only asserts the core-committer things, that they are not cc.
    pr = fake_github.make_pull_request(
        user="felipemontoya", owner="edx", repo="edx-platform", created_at=datetime(2020, 1, 1),
    )
    prj = pr.as_json()

    with reqctx:
        issue_id, _ = pull_request_changed(prj)

    issue = fake_jira.issues[issue_id]
    assert issue.labels == set()

    # Check that the Jira issue was started in "Needs Triage"
    assert issue.status == "Needs Triage"

    # Check the GitHub comment that was created.
    pr_comments = pr.list_comments()
    assert len(pr_comments) == 1
    body = pr_comments[0].body
    jira_link = "[{id}](https://openedx.atlassian.net/browse/{id})".format(id=issue_id)
    assert jira_link in body
    assert "Thanks for the pull request, @felipemontoya!" in body
    assert not is_comment_kind(BotComment.CORE_COMMITTER, body)
    assert not is_comment_kind(BotComment.NEED_CLA, body)
    assert is_comment_kind(BotComment.OK_TO_TEST, body)

    # Check the GitHub labels that got applied.
    assert pr.labels == {"needs triage", "open-source-contribution"}


EXAMPLE_PLATFORM_MAP_1_2 = {
    "child": {
        "id": "14522",
        "self": "https://openedx.atlassian.net/rest/api/2/customFieldOption/14522",
        "value": "Course Level Insights"
    },
    "id": "14209",
    "self": "https://openedx.atlassian.net/rest/api/2/customFieldOption/14209",
    "value": "Researcher & Data Experiences"
}

@pytest.mark.parametrize("with_epic", [False, True])
def test_blended_pr_opened_with_cla(with_epic, reqctx, sync_labels_fn, fake_github, fake_jira):
    pr = fake_github.make_pull_request(owner="edx", repo="some-code", user="tusbar", title="[BD-34] Something good")
    prj = pr.as_json()
    total_issues = 0
    if with_epic:
        epic = fake_jira.make_issue(
            project="BLENDED",
            blended_project_id="BD-34",
            blended_project_status_page="https://thewiki/bd-34",
            platform_map_1_2=EXAMPLE_PLATFORM_MAP_1_2,
        )
        total_issues += 1

    with reqctx:
        issue_id, anything_happened = pull_request_changed(prj)

    assert issue_id is not None
    assert issue_id.startswith("BLENDED-")
    assert anything_happened is True

    # Check the Jira issue that was created.
    assert len(fake_jira.issues) == total_issues + 1
    issue = fake_jira.issues[issue_id]
    assert issue.contributor_name == "Bertrand Marron"
    assert issue.customer == ["IONISx"]
    assert issue.pr_number == prj["number"]
    assert issue.repo == "edx/some-code"
    assert issue.url == prj["html_url"]
    assert issue.description == prj["body"]
    assert issue.issuetype == "Pull Request Review"
    assert issue.summary == prj["title"]
    assert issue.labels == {"blended"}
    if with_epic:
        assert issue.epic_link == epic.key
        assert issue.platform_map_1_2 == EXAMPLE_PLATFORM_MAP_1_2
    else:
        assert issue.epic_link is None
        assert issue.platform_map_1_2 is None

    # Check that the Jira issue is in Needs Triage.
    assert issue.status == "Needs Triage"

    # Check that we synchronized labels.
    sync_labels_fn.assert_called_once_with("edx/some-code")

    # Check the GitHub comment that was created.
    pr_comments = pr.list_comments()
    assert len(pr_comments) == 1
    body = pr_comments[0].body
    jira_link = "[{id}](https://openedx.atlassian.net/browse/{id})".format(id=issue_id)
    assert jira_link in body
    assert "Thanks for the pull request, @tusbar!" in body
    has_project_link = "the [BD-34](https://thewiki/bd-34) project page" in body
    assert has_project_link == with_epic
    assert is_comment_kind(BotComment.BLENDED, body)
    assert not is_comment_kind(BotComment.NEED_CLA, body)
    assert is_comment_kind(BotComment.OK_TO_TEST, body)

    # Check the GitHub labels that got applied.
    assert pr.labels == {"needs triage", "blended"}


def test_external_pr_rescanned(reqctx, fake_github, fake_jira):
    # Rescanning a pull request shouldn't do anything.

    # Make a pull request and process it.
    pr = fake_github.make_pull_request(user="tusbar")
    with reqctx:
        issue_id1, anything_happened1 = pull_request_changed(pr.as_json())

    assert anything_happened1 is True
    assert len(pr.list_comments()) == 1

    # Rescan the pull request.
    with reqctx:
        issue_id2, anything_happened2 = pull_request_changed(pr.as_json())

    assert issue_id2 == issue_id1
    assert anything_happened2 is False

    # No Jira issue was created.
    assert len(fake_jira.issues) == 1

    # No new GitHub comment was created.
    assert len(pr.list_comments()) == 1


def test_contractor_pr_opened(reqctx, fake_github, fake_jira):
    pr = fake_github.make_pull_request(user="joecontractor")
    prj = pr.as_json()

    with reqctx:
        issue_id, anything_happened = pull_request_changed(prj)

    assert issue_id is None
    assert anything_happened is True

    # No Jira issue was created.
    assert len(fake_jira.issues) == 0

    # Check the GitHub comment that was created.
    pr_comments = pr.list_comments()
    assert len(pr_comments) == 1
    body = pr_comments[0].body
    assert is_comment_kind(BotComment.CONTRACTOR, body)
    href = (
        'href="https://openedx-webhooks.herokuapp.com/github/process_pr' +
        '?repo={}'.format(prj["base"]["repo"]["full_name"].replace("/", "%2F")) +
        '&number={}"'.format(prj["number"])
    )
    assert href in body
    assert 'Create an OSPR issue for this pull request' in body


def test_contractor_pr_rescanned(reqctx, fake_github, fake_jira):
    pr = fake_github.make_pull_request(user="joecontractor")
    with reqctx:
        issue_id, anything_happened = pull_request_changed(pr.as_json())

    assert issue_id is None
    assert anything_happened is True

    # No Jira issue was created.
    assert len(fake_jira.issues) == 0

    # One GitHub comment was created.
    assert len(pr.list_comments()) == 1

    # Rescan it.  Nothing should happen.
    with reqctx:
        issue_id, anything_happened = pull_request_changed(pr.as_json())

    assert issue_id is None
    assert anything_happened is False

    # No Jira issue was created.
    assert len(fake_jira.issues) == 0

    # One GitHub comment was created.
    assert len(pr.list_comments()) == 1


def test_changing_pr_title(reqctx, fake_github, fake_jira):
    # After the Jira issue is created, changing the title of the pull request
    # will update the title of the issue.
    pr = fake_github.make_pull_request(
        user="tusbar",
        title="These are my changes, please take them.",
    )

    with reqctx:
        issue_id1, _ = pull_request_changed(pr.as_json())

    issue = fake_jira.issues[issue_id1]
    assert issue.summary == "These are my changes, please take them."
    # The bot made one comment on the PR.
    assert len(pr.list_comments()) == 1

    # Someone transitions the issue to a new state, and adds a label.
    issue.status = "Blocked by Other Work"
    issue.labels.add("my-label")

    # Author updates the title.
    pr.title = "This is the best!"
    with reqctx:
        issue_id2, _ = pull_request_changed(pr.as_json())

    assert issue_id2 == issue_id1
    issue = fake_jira.issues[issue_id2]
    # The issue title has changed.
    assert issue.summary == "This is the best!"
    # The bot didn't make another comment.
    assert len(pr.list_comments()) == 1
    # The issue shouldn't have changed status.
    assert issue.status == "Blocked by Other Work"
    # The issue should still have the ad-hoc label.
    assert "my-label" in issue.labels


def test_changing_pr_description(reqctx, fake_github, fake_jira):
    # After the Jira issue is created, changing the body of the pull request
    # will update the description of the issue.
    pr = fake_github.make_pull_request(
        user="tusbar",
        title="These are my changes, please take them.",
        body="Blah blah lots of description.",
    )

    with reqctx:
        issue_id1, _ = pull_request_changed(pr.as_json())

    issue = fake_jira.issues[issue_id1]
    assert issue.summary == "These are my changes, please take them."
    assert issue.description == "Blah blah lots of description."
    # The bot made one comment on the PR.
    assert len(pr.list_comments()) == 1

    # The issue is in the correct initial state.
    assert issue.status == "Needs Triage"

    # Someone changes the issue status.
    issue.status = "Blocked by Other Work"
    labels = pr.labels
    labels.remove("needs triage")
    labels.add("blocked by other work")
    pr.set_labels(labels)

    # Author updates the description of the PR.
    pr.body = "OK, now I am really describing things."
    with reqctx:
        issue_id2, _ = pull_request_changed(pr.as_json())

    assert issue_id2 == issue_id1
    issue = fake_jira.issues[issue_id2]
    # The issue title hasn't changed, but the description has.
    assert issue.summary == "These are my changes, please take them."
    assert issue.description == "OK, now I am really describing things."
    # The bot didn't make another comment.
    assert len(pr.list_comments()) == 1

    # The issue should still be in the changed status, and the PR labels should
    # still be right.
    assert issue.status == "Blocked by Other Work"
    assert pr.labels == {"blocked by other work", "open-source-contribution"}


def test_title_change_changes_jira_project(reqctx, fake_github, fake_jira):
    """
    A blended developer opens a PR, but forgets to put "[BD]" in the title.
    """
    # The blended project exists:
    epic = fake_jira.make_issue(
        project="BLENDED",
        blended_project_id="BD-34",
        blended_project_status_page="https://thewiki/bd-34",
        platform_map_1_2=EXAMPLE_PLATFORM_MAP_1_2,
    )

    # The developer makes a pull request, but forgets the right syntax in the title.
    pr = fake_github.make_pull_request(user="tusbar", title="This is for BD-34")

    with reqctx:
        ospr_id, anything_happened = pull_request_changed(pr.as_json())

    # An OSPR issue was made.
    assert ospr_id is not None
    assert ospr_id.startswith("OSPR-")
    assert anything_happened is True
    assert ospr_id in fake_jira.issues

    # Someone assigns an ad-hoc label to the PR.
    pr.repo.add_label(name="pretty")
    pr.labels.add("pretty")

    # The developer changes the title.
    pr.title = "This is for [BD-34]."
    with reqctx:
        issue_id, anything_happened = pull_request_changed(pr.as_json())

    assert anything_happened is True
    assert issue_id is not None
    assert issue_id.startswith("BLENDED-")

    # The original issue has been deleted.
    assert ospr_id not in fake_jira.issues

    # The bot comment now mentions the new issue.
    pr_comments = pr.list_comments()
    assert len(pr_comments) == 1
    body = pr_comments[0].body
    assert f"I've created [{issue_id}](" in body
    assert f"The original issue {ospr_id} has been deleted." in body

    # The new issue has all the Blended stuff.
    issue = fake_jira.issues[issue_id]
    prj = pr.as_json()
    assert issue.contributor_name == "Bertrand Marron"
    assert issue.customer == ["IONISx"]
    assert issue.pr_number == prj["number"]
    assert issue.repo == "an-org/a-repo"
    assert issue.url == prj["html_url"]
    assert issue.description == prj["body"]
    assert issue.issuetype == "Pull Request Review"
    assert issue.summary == prj["title"]
    assert issue.labels == {"blended"}
    assert issue.epic_link == epic.key
    assert issue.platform_map_1_2 == EXAMPLE_PLATFORM_MAP_1_2

    # Check that the Jira issue is in Needs Triage.
    assert issue.status == "Needs Triage"

    # The pull request has to be associated with the new issue.
    assert get_jira_issue_key(prj) == issue_id

    # The pull request still has the ad-hoc label.
    assert "pretty" in pr.labels


def test_title_change_but_issue_already_moved(reqctx, fake_github, fake_jira):
    """
    A blended developer opens a PR, but forgets to put "[BD]" in the title.
    In the meantime, someone already moved the OSPR issue to BLENDED.
    """
    # The blended project exists:
    epic = fake_jira.make_issue(
        project="BLENDED",
        blended_project_id="BD-34",
        blended_project_status_page="https://thewiki/bd-34",
    )

    # The developer makes a pull request, but forgets the right syntax in the title.
    pr = fake_github.make_pull_request(user="tusbar", title="This is for BD-34")

    with reqctx:
        ospr_id, anything_happened = pull_request_changed(pr.as_json())

    # An OSPR issue was made.
    assert ospr_id is not None
    assert ospr_id.startswith("OSPR-")
    assert anything_happened is True
    assert ospr_id in fake_jira.issues

    # Someone moves the Jira issue.
    issue = fake_jira.find_issue(ospr_id)
    fake_jira.move_issue(issue, "BLENDED")

    # The developer changes the title.
    pr.title = "This is for [BD-34]."
    with reqctx:
        issue_id, anything_happened = pull_request_changed(pr.as_json())

    assert anything_happened is True
    assert issue_id is not None
    assert issue_id.startswith("BLENDED-")

    # The original issue is still available, but with a new key.
    assert fake_jira.find_issue(ospr_id) is not None

    # The bot comment now mentions the new issue.
    pr_comments = pr.list_comments()
    assert len(pr_comments) == 1
    body = pr_comments[0].body
    assert f"I've created [{issue_id}](" in body
    # but doesn't say the old issue is deleted.
    assert "The original issue" not in body
    assert "More details are on" in body

    issue = fake_jira.issues[issue_id]
    prj = pr.as_json()
    assert issue.contributor_name == "Bertrand Marron"
    assert issue.customer == ["IONISx"]
    assert issue.pr_number == prj["number"]
    assert issue.repo == "an-org/a-repo"
    assert issue.url == prj["html_url"]
    assert issue.description == prj["body"]
    assert issue.issuetype == "Pull Request Review"
    assert issue.summary == prj["title"] == "This is for [BD-34]."
    assert issue.labels == {"blended"}
    assert issue.epic_link == epic.key

    # Check that the Jira issue is in Needs Triage.
    assert issue.status == "Needs Triage"

    # The pull request has to be associated with the new issue.
    assert get_jira_issue_key(prj) == issue_id


@pytest.mark.parametrize(
    "pr_type, jira_got_fiddled",
    itertools.product(
        ["normal", "blended", "committer", "nocla"],
        [False, True],
    )
)
def test_draft_pr_opened(pr_type, jira_got_fiddled, reqctx, fake_github, fake_jira):
    # Open a WIP pull request.
    title1 = "WIP: broken"
    title2 = "Fixed and done"
    if pr_type == "normal":
        initial_status = "Needs Triage"
        pr = fake_github.make_pull_request(user="tusbar", title=title1)
    elif pr_type == "blended":
        title1 = "[BD-34] Something good (WIP)"
        title2 = "[BD-34] Something good"
        initial_status = "Needs Triage"
        pr = fake_github.make_pull_request(user="tusbar", title=title1)
    elif pr_type == "committer":
        initial_status = "Waiting on Author"
        pr = fake_github.make_pull_request(user="felipemontoya", owner="edx", repo="edx-platform", title=title1)
    else:
        assert pr_type == "nocla"
        initial_status = "Community Manager Review"
        fake_github.make_user(login="new_contributor", name="Newb Contributor")
        pr = fake_github.make_pull_request(owner="edx", repo="edx-platform", user="new_contributor", title=title1)

    prj = pr.as_json()

    with reqctx:
        issue_id, anything_happened = pull_request_changed(prj)

    assert issue_id is not None
    assert issue_id.startswith("BLENDED-" if pr_type == "blended" else "OSPR-")
    assert anything_happened is True

    # Check the Jira issue that was created.
    assert len(fake_jira.issues) == 1
    issue = fake_jira.issues[issue_id]
    assert issue.issuetype == "Pull Request Review"
    assert issue.summary == prj["title"]
    if pr_type == "normal":
        assert issue.labels == set()
    elif pr_type == "blended":
        assert issue.labels == {"blended"}
    elif pr_type == "committer":
        assert issue.labels == {"core-committer"}
    else:
        assert pr_type == "nocla"
        assert issue.labels == set()

    # Because of "WIP", the Jira issue is in "Waiting on Author", unless
    # there's no CLA.
    if pr_type == "nocla":
        assert issue.status == "Community Manager Review"
    else:
        assert issue.status == "Waiting on Author"

    # Check the GitHub comment that was created.
    pr_comments = pr.list_comments()
    assert len(pr_comments) == 1
    body = pr_comments[0].body
    assert 'This is currently a draft pull request' in body
    assert 'click "Ready for Review"' in body
    if pr_type == "normal":
        assert is_comment_kind(BotComment.WELCOME, body)
        assert pr.labels == {"waiting on author", "open-source-contribution"}
    elif pr_type == "blended":
        assert is_comment_kind(BotComment.BLENDED, body)
        assert pr.labels == {"waiting on author", "blended"}
    elif pr_type == "committer":
        assert is_comment_kind(BotComment.CORE_COMMITTER, body)
        assert pr.labels == {"waiting on author", "core committer", "open-source-contribution"}
    else:
        assert pr_type == "nocla"
        assert is_comment_kind(BotComment.NEED_CLA, body)
        assert pr.labels == {
            'community manager review',
            'open-source-contribution',
            'NEED-CLA',
        }

    if jira_got_fiddled:
        # Someone changes the status from "Waiting on Author" manually.
        issue.status = "Architecture Review"

    # The author updates the PR, no longer draft.
    pr.title = title2
    with reqctx:
        issue_id2, _ = pull_request_changed(pr.as_json())

    assert issue_id2 == issue_id
    issue = fake_jira.issues[issue_id]
    assert issue.summary == title2

    pr_comments = pr.list_comments()
    assert len(pr_comments) == 1
    body = pr_comments[0].body
    assert 'This is currently a draft pull request' not in body
    assert 'click "Ready for Review"' not in body

    if jira_got_fiddled:
        assert issue.status == "Architecture Review"
        assert "architecture review" in pr.labels
        assert initial_status.lower() not in pr.labels
    else:
        assert issue.status == initial_status
        assert initial_status.lower() in pr.labels

    # Oops, it goes back to draft!
    pr.title = title1
    with reqctx:
        issue_id3, _ = pull_request_changed(pr.as_json())

    assert issue_id3 == issue_id
    issue = fake_jira.issues[issue_id]
    assert issue.summary == title1

    pr_comments = pr.list_comments()
    assert len(pr_comments) == 1
    body = pr_comments[0].body
    assert 'This is currently a draft pull request' in body
    assert 'click "Ready for Review"' in body

    if jira_got_fiddled:
        # We don't change the Jira status again if the PR goes back to draft.
        assert issue.status == "Architecture Review"
        assert "architecture review" in pr.labels
        assert initial_status.lower() not in pr.labels
    else:
        assert issue.status == initial_status
        assert initial_status.lower() in pr.labels


@pytest.mark.parametrize("merged", [False, True])
def test_handle_closed_pr(reqctx, sync_labels_fn, fake_github, fake_jira, merged):
    pr = fake_github.make_pull_request(user="tusbar", number=11237, state="closed", merged=merged)
    prj = pr.as_json()

    with reqctx:
        issue_id1, anything_happened = pull_request_changed(prj)

    assert issue_id1 is not None
    assert issue_id1.startswith("OSPR-")
    assert anything_happened is True

    # Check the Jira issue that was created.
    assert len(fake_jira.issues) == 1
    issue = fake_jira.issues[issue_id1]
    assert issue.contributor_name == "Bertrand Marron"
    assert issue.customer == ["IONISx"]
    assert issue.pr_number == 11237
    assert issue.url == prj["html_url"]
    assert issue.description == prj["body"]
    assert issue.issuetype == "Pull Request Review"
    assert issue.summary == prj["title"]
    assert issue.labels == set()

    # Check that the Jira issue is in the right state.
    assert issue.status == ("Merged" if merged else "Rejected")

    # Check the GitHub comment that was created.
    pr_comments = pr.list_comments()
    assert len(pr_comments) == 1
    body = pr_comments[0].body
    jira_link = "[{id}](https://openedx.atlassian.net/browse/{id})".format(id=issue_id1)
    assert jira_link in body
    if merged:
        assert "Although this pull request is already merged," in body
    else:
        assert "Although this pull request is already closed," in body
    assert is_comment_kind(BotComment.WELCOME, body)
    assert is_comment_kind(BotComment.WELCOME_CLOSED, body)
    assert not is_comment_kind(BotComment.NEED_CLA, body)
    assert is_comment_kind(BotComment.OK_TO_TEST, body)

    # Check the GitHub labels that got applied.
    assert pr.labels == {("merged" if merged else "rejected"), "open-source-contribution"}

    # Rescan the pull request.
    with reqctx:
        issue_id2, anything_happened2 = pull_request_changed(pr.as_json())

    assert issue_id2 == issue_id1
    assert anything_happened2 is False

    # No Jira issue was created.
    assert len(fake_jira.issues) == 1

    # No new GitHub comment was created.
    assert len(pr.list_comments()) == 1


def test_extra_fields_are_ok(reqctx, fake_github, fake_jira):
    # If someone adds platform map information to the Jira issue, it won't
    # trigger an update.
    pr = fake_github.make_pull_request(
        user="tusbar",
        title="These are my changes, please take them.",
        additions=1776,
        deletions=1492,
    )

    with reqctx:
        issue_id1, _ = pull_request_changed(pr.as_json())

    issue = fake_jira.issues[issue_id1]
    assert issue.summary == "These are my changes, please take them."
    # The bot made one comment on the PR.
    assert len(pr.list_comments()) == 1

    # Someone adds platform map and label to the Jira.
    issue.platform_map_1_2 = EXAMPLE_PLATFORM_MAP_1_2
    issue.labels.add("my-label")

    # PR gets rescanned.
    with reqctx:
        print("GOING AGAIN")
        issue_id2, happened = pull_request_changed(pr.as_json())

    assert not happened
    assert issue_id2 == issue_id1
    issue = fake_jira.issues[issue_id2]
    # The bot didn't make another comment.
    assert len(pr.list_comments()) == 1
    # The issue should still have the ad-hoc label.
    assert "my-label" in issue.labels
