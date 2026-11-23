"""The item kinds: validation and the rendered commands. No database.

Every value here is made up.
"""

from __future__ import annotations

from typing import get_args

import pytest
from pydantic import ValidationError

from factory_board import kinds
from factory_board.kinds import (
    Decision,
    Kubectl,
    Machine,
    Manual,
    MusicTool,
    OperatorKind,
    PrApprove,
    PrMerge,
    Session,
    Subject,
    SubjectType,
    TerraformApply,
    parse_operator_item,
)

SHA = "0123456789abcdef0123456789abcdef01234567"
OTHER_SHA = "89abcdef0123456789abcdef0123456789abcdef"
ISSUE = Subject(repo="example/repo", type=SubjectType.ISSUE, ref=101)
PR = Subject(repo="example/repo", type=SubjectType.PR, ref=202)


def item(kind: str, **fields: object) -> dict[str, object]:
    return {
        "kind": kind,
        "ticket": {"repo": "example/repo", "number": 101},
        "summary": "Restarts the widget service so it reads the new setting.",
        **fields,
    }


def kubectl(action: dict[str, object], **fields: object) -> dict[str, object]:
    return item("kubectl", action=action, **fields)


RESTART: dict[str, object] = {
    "verb": "rollout restart",
    "namespace": "widgets",
    "resource": "deployment",
    "name": "widget-api",
}


def refused(data: dict[str, object]) -> str:
    with pytest.raises(ValidationError) as excinfo:
        parse_operator_item(data)
    return str(excinfo.value)


# --- Names -----------------------------------------------------------------------------


def test_the_exported_names() -> None:
    assert kinds.OPERATOR_KINDS == (
        "pr-approve",
        "pr-merge",
        "terraform-apply",
        "kubectl",
        "music-tool",
        "decision",
        "session",
        "manual",
    )
    assert kinds.STAGE_KINDS == (
        "assess",
        "work",
        "review",
        "deploy-check",
        "retro",
        "health",
        "digest",
        "merge",
    )
    assert kinds.KINDS == kinds.OPERATOR_KINDS + kinds.STAGE_KINDS
    assert kinds.MACHINES == ("browser", "phone-vm", "workstation", "pilot")
    assert kinds.SUBJECT_TYPES == ("issue", "pr", "alert", "finding")
    assert kinds.TERRAFORM_MODULES == (
        "talos",
        "backblaze",
        "desec",
        "cloudflare",
        "tailscale",
        "hetzner",
        "uptimerobot",
    )
    assert kinds.KUBECTL_VERBS == ("rollout restart", "create job", "delete", "annotate")


def test_every_operator_kind_has_exactly_one_model() -> None:
    union = get_args(get_args(kinds.OperatorItem)[0])
    tags = [get_args(model.model_fields["kind"].annotation)[0] for model in union]
    assert tuple(tags) == kinds.OPERATOR_KINDS


def test_every_kubectl_verb_has_exactly_one_model() -> None:
    union = get_args(get_args(kinds.KubectlAction)[0])
    tags = [get_args(model.model_fields["verb"].annotation)[0] for model in union]
    assert tuple(tags) == kinds.KUBECTL_VERBS


@pytest.mark.parametrize("stage_kind", kinds.STAGE_KINDS)
def test_a_stage_kind_is_not_an_operator_kind(stage_kind: str) -> None:
    assert "does not match any of the expected tags" in refused(item(stage_kind))


def test_an_item_without_a_kind_is_refused() -> None:
    with pytest.raises(ValidationError, match="Unable to extract tag"):
        parse_operator_item({"ticket": {"repo": "example/repo", "number": 101}})


# --- Subject ---------------------------------------------------------------------------


def test_subject_labels() -> None:
    assert ISSUE.label == "issue example/repo#101"
    assert PR.label == "pr example/repo#202"
    alert = Subject(repo="example/repo", type=SubjectType.ALERT, ref="WidgetQueueStuck")
    assert alert.label == "alert example/repo WidgetQueueStuck"
    finding = Subject.model_validate(
        {"repo": "example/repo", "type": "finding", "ref": "docs:stale-note"}
    )
    assert finding.label == "finding example/repo docs:stale-note"


@pytest.mark.parametrize(
    "subject",
    [
        {"repo": "example/repo", "type": "issue", "ref": "101"},
        {"repo": "example/repo", "type": "pr", "ref": 0},
        {"repo": "example/repo", "type": "pr", "ref": True},
        {"repo": "example/repo", "type": "alert", "ref": 7},
        {"repo": "example/repo", "type": "finding", "ref": "two words"},
        {"repo": "example/repo", "type": "finding", "ref": "id\n"},
        {"repo": "example/repo", "type": "commit", "ref": 1},
        {"repo": "example", "type": "issue", "ref": 1},
        {"repo": "example/repo/extra", "type": "issue", "ref": 1},
        {"repo": "example/..", "type": "issue", "ref": 1},
        {"repo": "example/repo\n", "type": "issue", "ref": 1},
        {"repo": "example/repo", "type": "issue", "ref": 1, "title": "x"},
    ],
)
def test_a_bad_subject_is_refused(subject: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Subject.model_validate(subject)


# --- Each kind: its model, its command, its machine ------------------------------------


def test_pr_approve() -> None:
    parsed = parse_operator_item(
        item("pr-approve", repo="example/repo", number=202, head_sha=SHA, machine="workstation")
    )
    assert isinstance(parsed, PrApprove)
    assert parsed.subject == PR
    rendered = parsed.render(PR)
    assert rendered.kind is OperatorKind.PR_APPROVE
    assert rendered.machine is Machine.WORKSTATION
    assert rendered.commands == ("gh pr review 202 --repo example/repo --approve",)
    assert rendered.url == "https://github.com/example/repo/pull/202"
    assert rendered.free_text is False
    assert rendered.confirmation == (
        "pr-approve for pr example/repo#202: "
        "gh pr review 202 --repo example/repo --approve (head 0123456789ab)"
    )


def test_pr_approve_takes_browser_or_workstation_only() -> None:
    fields = {"repo": "example/repo", "number": 202, "head_sha": SHA}
    browser = parse_operator_item(item("pr-approve", machine="browser", **fields))
    assert browser.render(PR).machine is Machine.BROWSER
    refused(item("pr-approve", machine="pilot", **fields))
    refused(item("pr-approve", machine="phone-vm", **fields))
    assert "machine" in refused(item("pr-approve", **fields))


def test_pr_merge() -> None:
    parsed = parse_operator_item(item("pr-merge", repo="example/repo", number=202, head_sha=SHA))
    assert isinstance(parsed, PrMerge)
    rendered = parsed.render(PR)
    assert rendered.kind is OperatorKind.PR_MERGE
    assert rendered.machine is Machine.BROWSER
    assert rendered.commands == ()
    assert rendered.url == "https://github.com/example/repo/pull/202"
    assert rendered.confirmation == (
        "pr-merge for pr example/repo#202: "
        "merge https://github.com/example/repo/pull/202 (head 0123456789ab)"
    )


def test_pr_merge_does_not_take_a_machine() -> None:
    fields = {"repo": "example/repo", "number": 202, "head_sha": SHA}
    assert "Extra inputs are not permitted" in refused(
        item("pr-merge", machine="workstation", **fields)
    )


def test_the_head_sha_can_be_replaced() -> None:
    parsed = parse_operator_item(item("pr-merge", repo="example/repo", number=202, head_sha=SHA))
    assert isinstance(parsed, PrMerge)
    moved = parsed.with_head_sha(OTHER_SHA)
    assert isinstance(moved, PrMerge)
    assert moved.head_sha == OTHER_SHA
    assert parsed.head_sha == SHA
    assert moved.model_dump(exclude={"head_sha"}) == parsed.model_dump(exclude={"head_sha"})
    with pytest.raises(ValidationError):
        parsed.with_head_sha("0123abc")


@pytest.mark.parametrize(
    "head_sha",
    [
        "0123abc",
        SHA.upper(),
        SHA[:-1],
        SHA + "0",
        SHA[:-1] + "g",
        SHA + "\n",
        "",
    ],
)
def test_a_bad_head_sha_is_refused(head_sha: str) -> None:
    for kind, extra in (("pr-merge", {}), ("pr-approve", {"machine": "browser"})):
        message = refused(item(kind, repo="example/repo", number=202, head_sha=head_sha, **extra))
        assert "head_sha" in message


def test_terraform_apply() -> None:
    parsed = parse_operator_item(
        item(
            "terraform-apply",
            module="desec",
            pr={"repo": "example/repo", "number": 202},
            expected_plan="One record added, nothing changed or destroyed.",
        )
    )
    assert isinstance(parsed, TerraformApply)
    assert parsed.pr.url == "https://github.com/example/repo/pull/202"
    rendered = parsed.render(ISSUE)
    assert rendered.kind is OperatorKind.TERRAFORM_APPLY
    assert rendered.machine is Machine.WORKSTATION
    assert rendered.commands == ("just plan-desec", "just apply-desec")
    assert rendered.url is None
    assert rendered.confirmation == (
        "terraform-apply for issue example/repo#101: just plan-desec then just apply-desec"
    )


@pytest.mark.parametrize("module", kinds.TERRAFORM_MODULES)
def test_terraform_apply_renders_every_module(module: str) -> None:
    parsed = parse_operator_item(
        item(
            "terraform-apply",
            module=module,
            pr={"repo": "example/repo", "number": 202},
            expected_plan="Nothing destroyed.",
        )
    )
    assert parsed.render(ISSUE).commands == (f"just plan-{module}", f"just apply-{module}")


@pytest.mark.parametrize("module", ["widgets", "talos; true", "Talos", ""])
def test_an_unknown_terraform_module_is_refused(module: str) -> None:
    message = refused(
        item(
            "terraform-apply",
            module=module,
            pr={"repo": "example/repo", "number": 202},
            expected_plan="Nothing destroyed.",
        )
    )
    assert "module" in message


def test_terraform_apply_carries_no_plan() -> None:
    assert "Extra inputs are not permitted" in refused(
        item(
            "terraform-apply",
            module="desec",
            pr={"repo": "example/repo", "number": 202},
            expected_plan="Nothing destroyed.",
            plan="+ resource ...",
        )
    )


@pytest.mark.parametrize(
    ("action", "command"),
    [
        (RESTART, "kubectl rollout restart deployment/widget-api -n widgets"),
        (
            {**RESTART, "resource": "statefulset", "name": "widget-db"},
            "kubectl rollout restart statefulset/widget-db -n widgets",
        ),
        (
            {
                "verb": "create job",
                "namespace": "widgets",
                "cronjob": "widget-sweep",
                "job": "widget-sweep-manual-1",
            },
            "kubectl create job widget-sweep-manual-1 --from=cronjob/widget-sweep -n widgets",
        ),
        (
            {
                "verb": "delete",
                "namespace": "widgets",
                "resource": "certificates.example.io",
                "name": "widget.example.org",
            },
            "kubectl delete certificates.example.io widget.example.org -n widgets",
        ),
        (
            {"verb": "annotate", "namespace": "gitops", "application": "widgets"},
            "kubectl annotate applications.argoproj.io widgets "
            "argocd.argoproj.io/refresh=normal --overwrite -n gitops",
        ),
        (
            {"verb": "annotate", "namespace": "gitops", "application": "widgets", "hard": True},
            "kubectl annotate applications.argoproj.io widgets "
            "argocd.argoproj.io/refresh=hard --overwrite -n gitops",
        ),
    ],
)
def test_kubectl(action: dict[str, object], command: str) -> None:
    parsed = parse_operator_item(kubectl(action))
    assert isinstance(parsed, Kubectl)
    rendered = parsed.render(ISSUE)
    assert rendered.kind is OperatorKind.KUBECTL
    assert rendered.machine is Machine.PHONE_VM
    assert rendered.commands == (command,)
    assert rendered.commands[0].startswith("kubectl ")
    assert "KUBECONFIG" not in rendered.commands[0]
    assert rendered.url is None
    assert rendered.free_text is False
    assert rendered.confirmation == f"kubectl for issue example/repo#101: {command}"


def test_kubectl_break_glass_runs_on_the_workstation() -> None:
    parsed = parse_operator_item(
        kubectl(
            RESTART,
            break_glass=True,
            break_glass_reason="The phone's login service is down.",
        )
    )
    rendered = parsed.render(ISSUE)
    assert rendered.machine is Machine.WORKSTATION
    assert rendered.commands == ("kubectl rollout restart deployment/widget-api -n widgets",)
    assert "KUBECONFIG" not in rendered.commands[0]


def test_kubectl_break_glass_needs_a_reason() -> None:
    assert "break_glass needs a break_glass_reason" in refused(kubectl(RESTART, break_glass=True))
    refused(kubectl(RESTART, break_glass=True, break_glass_reason=""))
    assert "break_glass_reason is set but break_glass is not" in refused(
        kubectl(RESTART, break_glass_reason="The phone's login service is down.")
    )


@pytest.mark.parametrize("verb", ["exec", "patch", "apply", "scale", "rollout undo", "get", ""])
def test_a_fifth_kubectl_verb_is_refused(verb: str) -> None:
    assert "does not match any of the expected tags" in refused(kubectl({**RESTART, "verb": verb}))


@pytest.mark.parametrize(
    "name",
    [
        "Widget-Api",
        "widget_api",
        "-widget",
        "widget-",
        "widget..api",
        ".widget",
        "widget api",
        "",
        "w" * 254,
        # Smuggled shell and flags.
        "widget-api\n",
        "widget-api\nkubectl delete namespace widgets",
        "widget-api; true",
        "widget-api && true",
        "widget-api|true",
        "$(true)",
        "`true`",
        "widget-api>out",
        "widget-api --all",
        "--all",
        "widget-api\t",
        "widget-api\x00",
        "widget/api",
        "widget-\u0430pi",
    ],
)
def test_a_bad_kubernetes_name_is_refused(name: str) -> None:
    actions: list[dict[str, object]] = [
        {**RESTART, "name": name},
        {**RESTART, "namespace": name},
        {"verb": "create job", "namespace": "widgets", "cronjob": name, "job": "run-1"},
        {"verb": "create job", "namespace": "widgets", "cronjob": "widget-sweep", "job": name},
        {"verb": "delete", "namespace": "widgets", "resource": name, "name": "widget"},
        {"verb": "delete", "namespace": "widgets", "resource": "configmap", "name": name},
        {"verb": "annotate", "namespace": "gitops", "application": name},
        {"verb": "annotate", "namespace": name, "application": "widgets"},
    ]
    for action in actions:
        refused(kubectl(action))


def test_a_namespace_is_a_label_not_a_subdomain() -> None:
    refused(kubectl({**RESTART, "namespace": "widgets.example"}))
    refused(kubectl({**RESTART, "namespace": "w" * 64}))
    parse_operator_item(kubectl({**RESTART, "namespace": "w" * 63}))
    parse_operator_item(kubectl({**RESTART, "name": "w" * 63 + "." + "w" * 63}))


@pytest.mark.parametrize(
    "action",
    [
        {**RESTART, "resource": "pod"},
        {**RESTART, "resource": "deployment/widget-api"},
        {**RESTART, "selector": "app=widget"},
        {**RESTART, "args": ["--all"]},
        {"verb": "delete", "namespace": "widgets", "resource": "configmap", "name": "w", "all": 1},
        {"verb": "delete", "resource": "configmap", "name": "w"},
        {"verb": "create job", "namespace": "widgets", "cronjob": "widget-sweep"},
        {
            "verb": "annotate",
            "namespace": "gitops",
            "application": "widgets",
            "annotation": "example.org/owner=someone",
        },
        {"verb": "annotate", "namespace": "gitops", "application": "widgets", "hard": "yes"},
        {"verb": "annotate", "namespace": "gitops", "resource": "deployment", "name": "w"},
    ],
)
def test_kubectl_has_no_free_arguments(action: dict[str, object]) -> None:
    refused(kubectl(action))


def test_music_tool() -> None:
    parsed = parse_operator_item(
        item(
            "music-tool",
            tool="reset_widget",
            arguments={"album": 'An "Example" Album', "apply": False, "limit": 3, "ratio": 0.5},
        )
    )
    assert isinstance(parsed, MusicTool)
    rendered = parsed.render(ISSUE)
    assert rendered.kind is OperatorKind.MUSIC_TOOL
    assert rendered.machine is Machine.BROWSER
    command = 'reset_widget(album="An \\"Example\\" Album", apply=false, limit=3, ratio=0.5)'
    assert rendered.commands == (command,)
    assert rendered.confirmation == f"music-tool for issue example/repo#101: {command}"


def test_music_tool_without_arguments() -> None:
    parsed = parse_operator_item(item("music-tool", tool="start_scan"))
    assert parsed.render(ISSUE).commands == ("start_scan()",)
    nothing = parse_operator_item(item("music-tool", tool="start_scan", arguments={"x": None}))
    assert nothing.render(ISSUE).commands == ("start_scan(x=null)",)


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("reset-widget", {}),
        ("Reset", {}),
        ("reset widget", {}),
        ("reset_widget()", {}),
        ("reset_widget\n", {}),
        ("reset_widget", {"bad-key": 1}),
        ("reset_widget", {"album": ["a", "b"]}),
        ("reset_widget", {"album": {"nested": 1}}),
        ("reset_widget", {"album": "two\nlines"}),
        ("reset_widget", {"ratio": float("nan")}),
        ("reset_widget", {"ratio": float("inf")}),
        ("reset_widget", {f"k{n}": n for n in range(21)}),
    ],
)
def test_a_bad_music_tool_is_refused(tool: str, arguments: dict[str, object]) -> None:
    refused(item("music-tool", tool=tool, arguments=arguments))


def test_decision() -> None:
    parsed = parse_operator_item(
        item("decision", options=["Keep the old widget.", "Replace it with the new one."])
    )
    assert isinstance(parsed, Decision)
    assert parsed.options == ("Keep the old widget.", "Replace it with the new one.")
    rendered = parsed.render(ISSUE)
    assert rendered.kind is OperatorKind.DECISION
    assert rendered.machine is None
    assert rendered.commands == ()
    assert rendered.url is None
    assert rendered.confirmation == "decision for issue example/repo#101: choose one of 2 options"


@pytest.mark.parametrize(
    "options",
    [
        [],
        ["Only one option."],
        ["The same.", "The same."],
        ["Fine.", ""],
        ["Fine.", "Two\nlines."],
        ["Fine.", 3],
        [f"Option {n}." for n in range(11)],
    ],
)
def test_a_bad_decision_is_refused(options: list[object]) -> None:
    assert "options" in refused(item("decision", options=options))


def test_session() -> None:
    prompt = "Plan example/repo#101: split it into sub-issues and write the work doc."
    parsed = parse_operator_item(item("session", prompt=prompt))
    assert isinstance(parsed, Session)
    rendered = parsed.render(ISSUE)
    assert rendered.kind is OperatorKind.SESSION
    assert rendered.machine is Machine.PILOT
    assert rendered.commands == (prompt,)
    assert rendered.free_text is True
    assert rendered.confirmation == f"session for issue example/repo#101: free text: {prompt}"


def test_manual_is_marked_free_text() -> None:
    command = "widgetctl rebuild --index main && widgetctl status | head -3"
    parsed = parse_operator_item(item("manual", command=command, machine="workstation"))
    assert isinstance(parsed, Manual)
    rendered = parsed.render(ISSUE)
    assert rendered.kind is OperatorKind.MANUAL
    assert rendered.machine is Machine.WORKSTATION
    assert rendered.commands == (command,)
    assert rendered.free_text is True
    assert rendered.confirmation == f"manual for issue example/repo#101: free text: {command}"


def test_only_manual_and_session_are_free_text() -> None:
    examples: list[dict[str, object]] = [
        item("pr-approve", repo="example/repo", number=202, head_sha=SHA, machine="browser"),
        item("pr-merge", repo="example/repo", number=202, head_sha=SHA),
        item(
            "terraform-apply",
            module="desec",
            pr={"repo": "example/repo", "number": 202},
            expected_plan="Nothing destroyed.",
        ),
        kubectl(RESTART),
        item("music-tool", tool="start_scan", arguments={"album": "An Example"}),
        item("decision", options=["One.", "Two."]),
        item("session", prompt="Plan it."),
        item("manual", command="widgetctl status", machine="pilot"),
    ]
    assert [str(data["kind"]) for data in examples] == list(kinds.OPERATOR_KINDS)
    for data in examples:
        rendered = parse_operator_item(data).render(ISSUE)
        unchecked = data["kind"] in ("session", "manual")
        assert rendered.free_text is unchecked
        assert ("free text: " in rendered.confirmation) is unchecked


def test_the_summary_is_never_part_of_a_command() -> None:
    marker = "MARKER-IN-THE-SUMMARY"
    examples: list[dict[str, object]] = [
        item("pr-approve", repo="example/repo", number=202, head_sha=SHA, machine="browser"),
        item(
            "terraform-apply",
            module="desec",
            pr={"repo": "example/repo", "number": 202},
            expected_plan=marker,
        ),
        kubectl(RESTART, break_glass=True, break_glass_reason=marker),
        item("decision", options=[f"{marker} one.", f"{marker} two."]),
    ]
    for data in examples:
        rendered = parse_operator_item({**data, "summary": marker}).render(ISSUE)
        assert marker not in rendered.model_dump_json()


def test_manual_needs_a_known_machine() -> None:
    assert "machine" in refused(item("manual", command="widgetctl status"))
    assert "machine" in refused(item("manual", command="widgetctl status", machine="laptop"))


# --- Rules for every kind --------------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        item("pr-merge", repo="example/repo", number=202, head_sha=SHA, note="x"),
        item("pr-approve", repo="example/repo", number=202, head_sha=SHA, machine="browser", x=1),
        kubectl(RESTART, urgent=True),
        item("music-tool", tool="start_scan", apply=True),
        item("decision", options=["One.", "Two."], default=0),
        item("session", prompt="Plan it.", machine="pilot"),
        item("manual", command="widgetctl status", machine="pilot", free_text=False),
    ],
)
def test_an_unknown_field_is_refused(data: dict[str, object]) -> None:
    assert "Extra inputs are not permitted" in refused(data)


@pytest.mark.parametrize(
    "text",
    [
        "two\nlines",
        "carriage\rreturn",
        "a\ttab",
        "nul\x00byte",
        "escape\x1b[31m",
        "line\u2028separator",
        "paragraph\u2029separator",
        "bidi\u202eoverride",
        "zero\u200bwidth",
        " leading space",
        "trailing space ",
        "",
    ],
)
def test_text_is_one_clean_line(text: str) -> None:
    refused(item("manual", command=text, machine="pilot"))
    refused(item("session", prompt=text))
    refused(item("decision", options=["Fine.", text]))
    refused({**item("session", prompt="Plan it."), "summary": text})
    refused(
        item(
            "terraform-apply",
            module="desec",
            pr={"repo": "example/repo", "number": 202},
            expected_plan=text,
        )
    )
    refused(kubectl(RESTART, break_glass=True, break_glass_reason=text))
    refused(item("music-tool", tool="start_scan", arguments={"album": text or "\n"}))


def test_strings_are_length_bounded() -> None:
    refused({**item("session", prompt="Plan it."), "summary": "s" * 501})
    refused(item("session", prompt="p" * 2001))
    refused(item("manual", command="c" * 2001, machine="pilot"))
    refused(item("decision", options=["Fine.", "o" * 501]))
    refused(item("music-tool", tool="t" * 65))
    refused(item("music-tool", tool="start_scan", arguments={"album": "a" * 501}))
    parse_operator_item(item("session", prompt="p" * 2000))


@pytest.mark.parametrize(
    "ticket",
    [
        None,
        101,
        {"repo": "example/repo"},
        {"repo": "example/repo", "number": "101"},
        {"repo": "example/repo", "number": 0},
        {"repo": "example/repo", "number": -1},
        {"repo": "example/repo", "number": True},
        {"repo": "example/repo; true", "number": 101},
        {"repo": "example/repo", "number": 101, "title": "x"},
    ],
)
def test_a_bad_ticket_is_refused(ticket: object) -> None:
    assert "ticket" in refused({**item("session", prompt="Plan it."), "ticket": ticket})


def test_every_item_needs_a_ticket_and_a_summary() -> None:
    assert "ticket" in refused({"kind": "session", "summary": "Why.", "prompt": "Plan it."})
    assert "summary" in refused(
        {"kind": "session", "ticket": {"repo": "example/repo", "number": 101}, "prompt": "Plan."}
    )


@pytest.mark.parametrize(
    "repo", ["example", "example/", "/repo", "example/repo/pull", "-example/repo", "example/re po"]
)
def test_a_bad_pr_repo_is_refused(repo: str) -> None:
    assert "repo" in refused(item("pr-merge", repo=repo, number=202, head_sha=SHA))


def test_items_are_frozen() -> None:
    parsed = parse_operator_item(item("session", prompt="Plan it."))
    with pytest.raises(ValidationError, match="frozen"):
        parsed.summary = "Something else."  # pyright: ignore[reportAttributeAccessIssue]


def test_an_item_survives_a_json_round_trip() -> None:
    examples: list[dict[str, object]] = [
        item("pr-approve", repo="example/repo", number=202, head_sha=SHA, machine="browser"),
        item("pr-merge", repo="example/repo", number=202, head_sha=SHA),
        item(
            "terraform-apply",
            module="talos",
            pr={"repo": "example/repo", "number": 202},
            expected_plan="Nothing destroyed.",
        ),
        kubectl(RESTART, break_glass=True, break_glass_reason="The login service is down."),
        kubectl({"verb": "annotate", "namespace": "gitops", "application": "widgets"}),
        item("music-tool", tool="start_scan", arguments={"apply": True, "n": 1, "x": None}),
        item("decision", options=["One.", "Two."]),
        item("session", prompt="Plan it."),
        item("manual", command="widgetctl status", machine="phone-vm"),
    ]
    for data in examples:
        parsed = parse_operator_item(data)
        dumped = parsed.model_dump(mode="json")
        assert dumped["kind"] == data["kind"]
        assert parse_operator_item(dumped) == parsed
