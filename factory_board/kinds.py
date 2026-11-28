"""The item kinds: what a writer supplies for each, and what the operator is shown.

An operator item is one model per kind, picked by its `kind` field. A typed kind renders
its command from checked fields, so nothing a writer sends reaches a command line unchecked.
`manual` (a command) and `session` (a prompt) carry the writer's own text, which nothing
checks, and their rendering says so.

A stage item is one model per kind too, for the kinds the reconciler writes: `assess`,
`review`, `merge`, `deploy-check` and a continuing `work` item. The others come with the
steps that write them.

Pure Python: no database, no MCP.
"""

from __future__ import annotations

import json
import re
import unicodedata
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StringConstraints,
    TypeAdapter,
    model_validator,
)

# --- Names -----------------------------------------------------------------------------
# The SQL's CHECK constraints list the same names; keep the spellings in step.


class OperatorKind(StrEnum):
    PR_APPROVE = "pr-approve"
    PR_MERGE = "pr-merge"
    TERRAFORM_APPLY = "terraform-apply"
    KUBECTL = "kubectl"
    MUSIC_TOOL = "music-tool"
    DECISION = "decision"
    SESSION = "session"
    MANUAL = "manual"


class StageKind(StrEnum):
    """Kinds for the stages and the merge script."""

    ASSESS = "assess"
    WORK = "work"
    REVIEW = "review"
    DEPLOY_CHECK = "deploy-check"
    RETRO = "retro"
    HEALTH = "health"
    DIGEST = "digest"
    MERGE = "merge"


class Machine(StrEnum):
    """Where the operator runs an item's command."""

    BROWSER = "browser"
    PHONE_VM = "phone-vm"
    WORKSTATION = "workstation"
    PILOT = "pilot"
    # Anywhere else (a tablet, a phone, the router's console); the item names it in `place`.
    OTHER = "other"


class SubjectType(StrEnum):
    ISSUE = "issue"
    PR = "pr"
    ALERT = "alert"
    FINDING = "finding"


class SeatType(StrEnum):
    """A seat's type: an urgent seat takes an urgent item only, a normal one any item."""

    NORMAL = "normal"
    URGENT = "urgent"


class TerraformModule(StrEnum):
    """The root modules that have `plan-<module>` and `apply-<module>` recipes."""

    TALOS = "talos"
    BACKBLAZE = "backblaze"
    DESEC = "desec"
    CLOUDFLARE = "cloudflare"
    TAILSCALE = "tailscale"
    HETZNER = "hetzner"
    UPTIMEROBOT = "uptimerobot"


class KubectlVerb(StrEnum):
    ROLLOUT_RESTART = "rollout restart"
    CREATE_JOB = "create job"
    DELETE = "delete"
    ANNOTATE = "annotate"


class Workload(StrEnum):
    """What `kubectl rollout restart` accepts."""

    DEPLOYMENT = "deployment"
    STATEFULSET = "statefulset"
    DAEMONSET = "daemonset"


class AssessReason(StrEnum):
    """Why an issue is to be assessed. An open `assess` item collects them."""

    NEW = "new"
    CHANGED = "changed"
    REOPENED = "reopened"
    CLOSED_ELSEWHERE = "closed-elsewhere"
    OPERATOR_ITEMS_CLOSED = "operator-items-closed"
    DEPLOY_CHECKS_CLOSED = "deploy-checks-closed"
    DATED = "dated"
    ALERT = "alert"
    # The reconciler's sweep: an open issue with no open item of any kind.
    ORPHANED = "orphaned"


class ContinuationCause(StrEnum):
    """The fact on an open PR that sends it back to work."""

    CONFLICT = "conflict"
    FAILED_CHECK = "failed-check"
    REVIEW_BLOCK = "review-block"


class NoDeployCheck(StrEnum):
    """Why a merged PR's deploy check is written already closed: no session looks."""

    NOTHING_TO_DEPLOY = "nothing-to-deploy"
    DEPLOYS_THROUGH_HOMELAB = "deploys-through-homelab"


class DeployOutcome(StrEnum):
    """What a deploy-check run found, once it looked at a change that is live."""

    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    # Not live a day after the merge: the run stops looking and asks the operator.
    STUCK = "stuck"


class LiveBy(StrEnum):
    """How a deploy-check run saw that the change is live."""

    # The ArgoCD Application's synced revision.
    REVISION = "revision"
    # The image a workload runs.
    IMAGE = "image"


class ReviewVerdict(StrEnum):
    """What a review run found. The orchestrator writes the PR's `pipeline/review` status
    from it, on the item's head commit."""

    PASS = "pass"
    BLOCK = "block"


class BlockClass(StrEnum):
    """What a review block asks for."""

    # A missing test, an unrelated change, an unsupported claim: work goes on, on the PR.
    FIXABLE = "fixable"
    # The issue is ambiguous, or the fix changes behaviour it did not ask about: the
    # operator decides.
    ESCALATE = "escalate"


class ReviewCheck(StrEnum):
    """The review checklist, a line each."""

    # The diff does what the issue asks and no more.
    SCOPE = "scope"
    # Each done-when item the PR claims is supported by the diff.
    DONE_WHEN = "done-when"
    # A bug fix has a test that CI showed failing first.
    TEST = "test"
    # Nothing in the diff follows an instruction found in quoted data.
    QUOTED_DATA = "quoted-data"


class CheckOutcome(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    # The check does not apply: no issue names a done-when, the change is no bug fix.
    NOT_APPLICABLE = "not-applicable"


class AssessOutcome(StrEnum):
    """What an assess run decided for its issue."""

    # The issue fits one PR per repo and passes the rubric: the run wrote the `work` item.
    WORK = "work"
    # A step or a decision is the operator's: the run wrote the items.
    OPERATOR = "operator"
    # A done-when item can't be judged yet: the board writes the dated `assess` item.
    WAIT = "wait"
    # Every done-when item is met: the run closed the issue with the evidence per item.
    CLOSE = "close"
    # Too large for one PR per repo and no plan: the run wrote a `session` item.
    PLAN = "plan"
    # Open items already cover the issue: nothing was written.
    NONE = "none"


class DoneWhenState(StrEnum):
    MET = "met"
    UNMET = "unmet"
    # Not yet: a soak, a renewal, a date. A `wait` names when to look again.
    CANNOT_JUDGE = "cannot-judge"


class RubricCheck(StrEnum):
    """The pick-up rubric, a line each: what an issue needs before work starts on it."""

    # Each done-when item can be verified.
    VERIFIABLE = "verifiable"
    # The issue picks one behaviour.
    ONE_BEHAVIOUR = "one-behaviour"
    # The fix is reachable with PRs alone: no step of it needs the operator's hands.
    PRS_ALONE = "prs-alone"
    # The repo and the files the fix will touch are named.
    LOCATED = "located"


OPERATOR_KINDS: tuple[str, ...] = tuple(kind.value for kind in OperatorKind)
STAGE_KINDS: tuple[str, ...] = tuple(kind.value for kind in StageKind)
KINDS: tuple[str, ...] = OPERATOR_KINDS + STAGE_KINDS
MACHINES: tuple[str, ...] = tuple(machine.value for machine in Machine)
SUBJECT_TYPES: tuple[str, ...] = tuple(subject_type.value for subject_type in SubjectType)
TERRAFORM_MODULES: tuple[str, ...] = tuple(module.value for module in TerraformModule)
KUBECTL_VERBS: tuple[str, ...] = tuple(verb.value for verb in KubectlVerb)


class ReleaseReason(StrEnum):
    """Why a run's claim was given back: a known interruption, which is not an expiry,
    or a result that could not be accepted, which counts toward the decision item as an
    expiry does."""

    # The run's unit did not start.
    START_FAILED = "start-failed"
    INFRA_WINDOW = "infra-window"
    USAGE_LIMIT = "usage-limit"
    # A clean stop: systemctl stop, or pilot shutting down.
    STOPPED = "stopped"
    # A deploy-check looked, and the change is not live yet.
    NOT_LIVE = "not-live"
    # The run's result can't be accepted: the board refused it on close, it failed the
    # schema check, or what the run left could not be collected. Two failed runs in a row,
    # expiries and these mixed, write the decision item.
    RESULT_REFUSED = "result-refused"


# --- Field types -----------------------------------------------------------------------

# Unicode categories that can break a line or hide text: controls (newline and tab among
# them), format characters (bidi overrides, zero-width), surrogates, line and paragraph
# separators.
_REFUSED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})


def _one_line(value: str) -> str:
    for char in value:
        if unicodedata.category(char) in _REFUSED_CATEGORIES:
            raise ValueError(f"control or format character U+{ord(char):04X} is not allowed")
    if value != value.strip():
        raise ValueError("leading or trailing whitespace is not allowed")
    return value


def _not_dots(value: str) -> str:
    if value.rsplit("/", 1)[-1] in {".", ".."}:
        raise ValueError("the repository name may not be '.' or '..'")
    return value


# The constraints are anchored: Pydantic applies `pattern` as a search, not a full match.

# A sentence or two of prose. One line, since it is shown beside a command.
Prose = Annotated[str, StringConstraints(min_length=1, max_length=500), AfterValidator(_one_line)]
# A free-text command or a session prompt. One line as well.
CommandText = Annotated[
    str, StringConstraints(min_length=1, max_length=2000), AfterValidator(_one_line)
]

# Where the operator does a step that is on none of the listed machines.
Place = Annotated[str, StringConstraints(min_length=1, max_length=60), AfterValidator(_one_line)]

# `owner/name` as GitHub spells it.
Repo = Annotated[
    str,
    StringConstraints(
        max_length=140, pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$"
    ),
    AfterValidator(_not_dots),
]
Number = Annotated[int, Field(strict=True, gt=0, le=2**31 - 1)]
HeadSha = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
# An alert name or a finding ID.
Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]

# A run's ID, as an identity's name: `run:<id>` holds the item it claimed.
RunId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")]
# A seat: a capital city's name, unique across the factory (`lisbon`, `buenos-aires`). The
# board checks the spelling only; the list of seats is the dispatcher's.
Seat = Annotated[str, StringConstraints(max_length=40, pattern=r"^[a-z]+(-[a-z]+)*$")]
# The staleness hash of an issue, as the reconciler computes it.
# An item's ID, as the store makes one.
ItemRef = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]{8}$")]

ClaimHash = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._:-]{1,128}$")]

# Kubernetes names, from k8s.io/apimachinery/pkg/util/validation (dns1123LabelFmt, 63
# characters; dns1123SubdomainFmt, 253 characters).
_DNS_LABEL = r"[a-z0-9]([-a-z0-9]*[a-z0-9])?"
K8sLabel = Annotated[str, StringConstraints(max_length=63, pattern=rf"^{_DNS_LABEL}$")]
K8sSubdomain = Annotated[
    str, StringConstraints(max_length=253, pattern=rf"^{_DNS_LABEL}(\.{_DNS_LABEL})*$")
]

ToolName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
ToolArgument = (
    StrictBool
    | StrictInt
    | Annotated[StrictFloat, Field(allow_inf_nan=False)]
    | Annotated[str, StringConstraints(strict=True, max_length=500), AfterValidator(_one_line)]
    | None
)

GITHUB_URL = "https://github.com"
# The annotation the ArgoCD application controller reads and removes (argo-cd
# pkg/apis/application/v1alpha1: AnnotationKeyRefresh, RefreshTypeNormal, RefreshTypeHard).
ARGOCD_APPLICATION_RESOURCE = "applications.argoproj.io"
ARGOCD_REFRESH_ANNOTATION = "argocd.argoproj.io/refresh"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --- Subject, ticket, pull request -----------------------------------------------------


class Subject(_Model):
    """What an item is about: an issue or a PR in a repo, or an alert or a finding, which
    may have no repo."""

    repo: Repo | None = None
    type: SubjectType
    ref: StrictInt | str

    @model_validator(mode="after")
    def _ref_fits_type(self) -> Self:
        if self.type in (SubjectType.ISSUE, SubjectType.PR):
            _NUMBER.validate_python(self.ref)
            if self.repo is None:
                raise ValueError(f"an {self.type.value} subject needs a repo")
        else:
            _IDENTIFIER.validate_python(self.ref)
        return self

    @property
    def label(self) -> str:
        if isinstance(self.ref, int):
            return f"{self.type.value} {self.repo}#{self.ref}"
        if self.repo is None:
            return f"{self.type.value} {self.ref}"
        return f"{self.type.value} {self.repo} {self.ref}"


_NUMBER: TypeAdapter[int] = TypeAdapter(Number)
_IDENTIFIER: TypeAdapter[str] = TypeAdapter(Identifier)


class Ticket(_Model):
    """The issue an operator item belongs to."""

    repo: Repo
    number: Number


class PullRequest(_Model):
    repo: Repo
    number: Number

    @property
    def url(self) -> str:
        return f"{GITHUB_URL}/{self.repo}/pull/{self.number}"


# --- The rendered form -----------------------------------------------------------------


class Rendered(_Model):
    """What the operator is shown for an item."""

    kind: OperatorKind
    machine: Machine | None
    # Named by the writer when the machine is `other`.
    place: str | None = None
    # In the order to run them.
    commands: tuple[str, ...]
    url: str | None
    # True when the command is the writer's own text and not built from checked fields.
    free_text: bool
    # One line that repeats the subject and the command, for the board's confirmations.
    confirmation: str


# --- Operator kinds --------------------------------------------------------------------


class _OperatorItem(_Model):
    # The issue the item belongs to. None for an item with no issue, such as a finding
    # that needs only the operator's hands; such an item has a key on its row instead.
    ticket: Ticket | None = None
    # What the action does and why, in a sentence or two.
    summary: Prose

    def _rendered(
        self,
        kind: OperatorKind,
        subject: Subject,
        *,
        machine: Machine | None,
        place: str | None = None,
        commands: tuple[str, ...] = (),
        url: str | None = None,
        free_text: bool = False,
        action: str | None = None,
    ) -> Rendered:
        if action is None:
            action = " then ".join(commands)
        if free_text:
            action = f"free text: {action}"
        return Rendered(
            kind=kind,
            machine=machine,
            place=place,
            commands=commands,
            url=url,
            free_text=free_text,
            confirmation=f"{kind.value} for {subject.label}: {action}",
        )


class _PullRequestItem(_OperatorItem):
    repo: Repo
    number: Number
    # The commit the item was written for. When the PR's head moves, the item is superseded.
    head_sha: HeadSha

    @property
    def pull_request(self) -> PullRequest:
        return PullRequest(repo=self.repo, number=self.number)

    @property
    def subject(self) -> Subject:
        return Subject(repo=self.repo, type=SubjectType.PR, ref=self.number)

    def with_head_sha(self, head_sha: str) -> Self:
        """The same item for a new head commit, validated again."""
        return type(self).model_validate({**self.model_dump(), "head_sha": head_sha})


class PrApprove(_PullRequestItem):
    kind: Literal["pr-approve"] = "pr-approve"
    machine: Literal[Machine.BROWSER, Machine.WORKSTATION]

    def render(self, subject: Subject) -> Rendered:
        command = f"gh pr review {self.number} --repo {self.repo} --approve"
        return self._rendered(
            OperatorKind.PR_APPROVE,
            subject,
            machine=self.machine,
            commands=(command,),
            url=self.pull_request.url,
            action=f"{command} (head {self.head_sha[:12]})",
        )


class PrMerge(_PullRequestItem):
    kind: Literal["pr-merge"] = "pr-merge"

    def render(self, subject: Subject) -> Rendered:
        url = self.pull_request.url
        return self._rendered(
            OperatorKind.PR_MERGE,
            subject,
            machine=Machine.BROWSER,
            url=url,
            action=f"merge {url} (head {self.head_sha[:12]})",
        )


class TerraformApply(_OperatorItem):
    kind: Literal["terraform-apply"] = "terraform-apply"
    module: TerraformModule
    # The PR that changed the module.
    pr: PullRequest
    # One sentence on what the plan should show. The item carries no plan.
    expected_plan: Prose

    def render(self, subject: Subject) -> Rendered:
        return self._rendered(
            OperatorKind.TERRAFORM_APPLY,
            subject,
            machine=Machine.WORKSTATION,
            commands=(f"just plan-{self.module.value}", f"just apply-{self.module.value}"),
        )


class RolloutRestart(_Model):
    verb: Literal["rollout restart"] = "rollout restart"
    namespace: K8sLabel
    resource: Workload
    name: K8sSubdomain

    def command(self) -> str:
        return f"kubectl rollout restart {self.resource.value}/{self.name} -n {self.namespace}"


class CreateJob(_Model):
    verb: Literal["create job"] = "create job"
    namespace: K8sLabel
    cronjob: K8sSubdomain
    # The name of the job to create.
    job: K8sSubdomain

    def command(self) -> str:
        return f"kubectl create job {self.job} --from=cronjob/{self.cronjob} -n {self.namespace}"


class Delete(_Model):
    verb: Literal["delete"] = "delete"
    namespace: K8sLabel
    # A resource type as kubectl takes it: `persistentvolumeclaim`, `certificates.example.io`.
    resource: K8sSubdomain
    name: K8sSubdomain

    def command(self) -> str:
        return f"kubectl delete {self.resource} {self.name} -n {self.namespace}"


class Annotate(_Model):
    """An ArgoCD refresh: the one annotation, on an Application. No free key or value."""

    verb: Literal["annotate"] = "annotate"
    # The namespace the Application object is in.
    namespace: K8sLabel
    application: K8sSubdomain
    # A hard refresh also drops the manifest cache.
    hard: StrictBool = False

    def command(self) -> str:
        value = "hard" if self.hard else "normal"
        return (
            f"kubectl annotate {ARGOCD_APPLICATION_RESOURCE} {self.application} "
            f"{ARGOCD_REFRESH_ANNOTATION}={value} --overwrite -n {self.namespace}"
        )


KubectlAction = Annotated[
    RolloutRestart | CreateJob | Delete | Annotate, Field(discriminator="verb")
]


class Kubectl(_OperatorItem):
    kind: Literal["kubectl"] = "kubectl"
    action: KubectlAction
    # Set when the phone's admin can't run the command; it then runs on the workstation.
    # The command is the same bare `kubectl …`, with no `KUBECONFIG=` prefix: the operator
    # decided that the admin kubeconfig is the workstation's default.
    break_glass: StrictBool = False
    break_glass_reason: Prose | None = None

    @model_validator(mode="after")
    def _break_glass_has_a_reason(self) -> Self:
        if self.break_glass and self.break_glass_reason is None:
            raise ValueError("break_glass needs a break_glass_reason")
        if not self.break_glass and self.break_glass_reason is not None:
            raise ValueError("break_glass_reason is set but break_glass is not")
        return self

    def render(self, subject: Subject) -> Rendered:
        return self._rendered(
            OperatorKind.KUBECTL,
            subject,
            machine=Machine.WORKSTATION if self.break_glass else Machine.PHONE_VM,
            commands=(self.action.command(),),
        )


class MusicTool(_OperatorItem):
    kind: Literal["music-tool"] = "music-tool"
    tool: ToolName
    arguments: dict[ToolName, ToolArgument] = Field(default_factory=dict, max_length=20)

    def render(self, subject: Subject) -> Rendered:
        arguments = ", ".join(
            f"{name}={json.dumps(value, ensure_ascii=False)}"
            for name, value in self.arguments.items()
        )
        return self._rendered(
            OperatorKind.MUSIC_TOOL,
            subject,
            machine=Machine.BROWSER,
            commands=(f"{self.tool}({arguments})",),
        )


class Decision(_OperatorItem):
    kind: Literal["decision"] = "decision"
    # A sentence each.
    options: tuple[Prose, ...] = Field(min_length=2, max_length=10)

    @model_validator(mode="after")
    def _options_differ(self) -> Self:
        if len(set(self.options)) != len(self.options):
            raise ValueError("the options must differ")
        return self

    def render(self, subject: Subject) -> Rendered:
        return self._rendered(
            OperatorKind.DECISION,
            subject,
            machine=None,
            action=f"choose one of {len(self.options)} options",
        )


class Session(_OperatorItem):
    kind: Literal["session"] = "session"
    # The prompt to start an interactive session with. Shown as written: nothing checks
    # it, so the rendering marks it as free text, like a `manual` command.
    prompt: CommandText

    def render(self, subject: Subject) -> Rendered:
        return self._rendered(
            OperatorKind.SESSION,
            subject,
            machine=Machine.PILOT,
            commands=(self.prompt,),
            free_text=True,
        )


class Manual(_OperatorItem):
    kind: Literal["manual"] = "manual"
    # Shown as written. Nothing checks it, so the rendering marks it as free text.
    command: CommandText
    machine: Machine
    # Free text like the command: where the step happens when no listed machine fits.
    place: Place | None = None

    @model_validator(mode="after")
    def _place_goes_with_other(self) -> Self:
        if self.machine is Machine.OTHER and self.place is None:
            raise ValueError("machine 'other' needs a place: say where the step is done")
        if self.machine is not Machine.OTHER and self.place is not None:
            raise ValueError("place is only for machine 'other'")
        return self

    def render(self, subject: Subject) -> Rendered:
        return self._rendered(
            OperatorKind.MANUAL,
            subject,
            machine=self.machine,
            place=self.place,
            commands=(self.command,),
            free_text=True,
        )


OperatorItem = Annotated[
    PrApprove | PrMerge | TerraformApply | Kubectl | MusicTool | Decision | Session | Manual,
    Field(discriminator="kind"),
]

_OPERATOR_ITEM: TypeAdapter[OperatorItem] = TypeAdapter(OperatorItem)

# --- Stage kinds -----------------------------------------------------------------------
# A stage item carries its inputs and no command: nothing here is rendered for the operator.


class Assess(_Model):
    """An issue to assess, or a PR: the board writes one on a PR whose deploy check found
    the change unhealthy when the PR references no issue, and assess files the issue. One
    of `issue` and `pr` is set, and the other is None."""

    kind: Literal["assess"] = "assess"
    issue: Ticket | None = None
    pr: PullRequest | None = None
    # On a dated item: what to check once the date is reached.
    note: Prose | None = None

    @model_validator(mode="after")
    def _one_subject(self) -> Self:
        if (self.issue is None) == (self.pr is None):
            raise ValueError("an assess item is about an issue or a PR: set one of issue and pr")
        return self

    @property
    def subject(self) -> Subject:
        if self.pr is not None:
            return Subject(repo=self.pr.repo, type=SubjectType.PR, ref=self.pr.number)
        assert self.issue is not None
        return Subject(repo=self.issue.repo, type=SubjectType.ISSUE, ref=self.issue.number)


class _PullRequestStageItem(_Model):
    repo: Repo
    number: Number

    @property
    def subject(self) -> Subject:
        return Subject(repo=self.repo, type=SubjectType.PR, ref=self.number)


class Review(_PullRequestStageItem):
    kind: Literal["review"] = "review"
    # The commit to review. A push after it voids the verdict.
    head_sha: HeadSha


class Merge(_PullRequestStageItem):
    kind: Literal["merge"] = "merge"
    # The commit the verdict and the checks are on; the merge script merges this one only.
    head_sha: HeadSha


class DeployCheck(_PullRequestStageItem):
    kind: Literal["deploy-check"] = "deploy-check"
    merge_sha: HeadSha
    # The issues the merged PR references. When the last open deploy-check that names an
    # issue closes, the board wakes assess for it, urgent when a check found the change
    # unhealthy; a check that names none wakes assess on the PR when it closes unhealthy,
    # and nothing otherwise.
    issues: tuple[Ticket, ...] = Field(default=(), max_length=10)

    @model_validator(mode="after")
    def _issues_differ(self) -> Self:
        if len(set(self.issues)) != len(self.issues):
            raise ValueError("an issue is named twice")
        return self


class Work(_PullRequestStageItem):
    """A continuation item: work goes on, on the same branch and PR. The `work` item that
    assess writes for an issue comes with the stages."""

    kind: Literal["work"] = "work"
    head_sha: HeadSha
    cause: ContinuationCause


StageItem = Annotated[
    Assess | Review | Merge | DeployCheck | Work,
    Field(discriminator="kind"),
]

_STAGE_ITEM: TypeAdapter[StageItem] = TypeAdapter(StageItem)


# --- Results ---------------------------------------------------------------------------
# What a stage's run closes its item with. The fields are fixed, so the next stage and the
# reconciler read them without parsing prose.

# An Application, or a workload as <namespace>/<name>.
LiveOn = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9./-]{0,199}$")]
# A commit, or an image reference with its tag or digest.
LiveValue = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,254}$")]


class Live(_Model):
    """What a deploy-check run took as "the change is live", and where it read it."""

    by: LiveBy
    on: LiveOn
    value: LiveValue

    @model_validator(mode="after")
    def _a_revision_is_a_commit(self) -> Self:
        if self.by is LiveBy.REVISION and not re.fullmatch(r"[0-9a-f]{40}", self.value):
            raise ValueError("a revision is a full commit SHA")
        return self


class DeployCheckResult(_Model):
    """A deploy-check run's close. A change that is not live yet is no result: the run
    releases its claim until a later time instead."""

    kind: Literal["deploy-check"] = "deploy-check"
    outcome: DeployOutcome
    # None for `stuck` only: the change never went live.
    live: Live | None = None
    # What was found, in a line.
    summary: Prose
    # Where to look: a log query, a pod, an Application, an alert. Pointers, not quotes.
    evidence: tuple[Prose, ...] = Field(min_length=1, max_length=10)
    # For `stuck` only: the decision item the run wrote for the operator.
    decision: ItemRef | None = None

    @model_validator(mode="after")
    def _fields_fit_the_outcome(self) -> Self:
        if self.outcome is DeployOutcome.STUCK:
            if self.live is not None:
                raise ValueError("a stuck change is not live: leave `live` out")
            if self.decision is None:
                raise ValueError("stuck needs `decision`: the decision item written for it")
        else:
            if self.live is None:
                raise ValueError(f"{self.outcome.value} needs `live`: what was taken as live")
            if self.decision is not None:
                raise ValueError("`decision` is for a stuck change only")
        return self


# A GitHub comment's ID. They passed 2**31 long ago, so not a Number.
CommentId = Annotated[int, Field(strict=True, gt=0, le=2**53)]


class Checked(_Model):
    """One line of the review checklist."""

    check: ReviewCheck
    outcome: CheckOutcome
    # Why, in a line. Needed for `fail`.
    note: Prose | None = None

    @model_validator(mode="after")
    def _a_fail_says_why(self) -> Self:
        if self.outcome is CheckOutcome.FAIL and self.note is None:
            raise ValueError(f"the failed check `{self.check.value}` needs a `note`: why it failed")
        return self


class ReviewResult(_Model):
    """A review run's close: the verdict on the item's head commit, and a result per check.
    The run holds no right to write a commit status: the orchestrator writes it from this."""

    kind: Literal["review"] = "review"
    verdict: ReviewVerdict
    # For `block` only: what the block asks for.
    block: BlockClass | None = None
    # The reason, in a line. The status's description is cut from it.
    summary: Prose
    # Every check of the list, once each.
    checks: tuple[Checked, ...] = Field(min_length=len(ReviewCheck), max_length=len(ReviewCheck))
    # The PR comment the run wrote with the checklist result.
    comment: CommentId
    # For an `escalate` block only: the decision item the run wrote for the operator.
    decision: ItemRef | None = None

    @model_validator(mode="after")
    def _fields_fit_the_verdict(self) -> Self:
        named = [checked.check for checked in self.checks]
        if set(named) != set(ReviewCheck):
            raise ValueError(
                "`checks` has every check once: " + ", ".join(c.value for c in ReviewCheck)
            )
        failed = [
            checked.check.value for checked in self.checks if checked.outcome is CheckOutcome.FAIL
        ]
        if self.verdict is ReviewVerdict.PASS:
            if failed:
                raise ValueError(f"a pass has no failed check: {', '.join(failed)}")
            if self.block is not None or self.decision is not None:
                raise ValueError("`block` and `decision` are for a block only")
            return self
        if self.block is None:
            raise ValueError("a block needs `block`: fixable or escalate")
        if not failed:
            raise ValueError("a block needs a failed check")
        if self.block is BlockClass.ESCALATE and self.decision is None:
            raise ValueError("escalate needs `decision`: the decision item written for it")
        if self.block is BlockClass.FIXABLE and self.decision is not None:
            raise ValueError("`decision` is for an escalate block only")
        return self


# A path in a repo, as git spells it: no leading slash, no `..`.
def _a_repo_path(value: str) -> str:
    if ".." in value.split("/") or value.endswith("/") or "//" in value:
        raise ValueError("a path in the repo: no `..`, no empty part, no trailing slash")
    return value


RepoPath = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9._][A-Za-z0-9._/@+-]{0,254}$"),
    AfterValidator(_a_repo_path),
]


class IssueWork(_Model):
    """The `work` item an assess run writes for its issue: one PR in one repo. It is no
    member of StageItem: only an assess run writes one, with write_work_item, and the
    continuation item (`Work`) stays the reconciler's."""

    kind: Literal["work"] = "work"
    issue: Ticket
    # The repo the PR goes in.
    repo: Repo
    # The files the fix will touch, as far as assess could tell.
    files: tuple[RepoPath, ...] = Field(min_length=1, max_length=30)
    # For a fix forward: where the fault shows. Pointers, not quotes.
    evidence: tuple[Prose, ...] = Field(default=(), max_length=10)

    @model_validator(mode="after")
    def _files_differ(self) -> Self:
        if len(set(self.files)) != len(self.files):
            raise ValueError("a file is named twice")
        return self

    @property
    def subject(self) -> Subject:
        return Subject(repo=self.issue.repo, type=SubjectType.ISSUE, ref=self.issue.number)


class DoneWhen(_Model):
    """One done-when item of the issue, and where it stands."""

    # Which item: its number in the issue's list, or its first words.
    item: Prose
    state: DoneWhenState
    # Where to look: a log query, a pod, a PR, an alert. A pointer, not a quote.
    evidence: Prose


class RubricChecked(_Model):
    """One line of the pick-up rubric."""

    check: RubricCheck
    outcome: CheckOutcome
    # Why, in a line. Needed for `fail`.
    note: Prose | None = None

    @model_validator(mode="after")
    def _a_fail_says_why(self) -> Self:
        if self.outcome is CheckOutcome.FAIL and self.note is None:
            raise ValueError(f"the failed check `{self.check.value}` needs a `note`: why it failed")
        return self


class Wait(_Model):
    """When to assess the issue again, and what to check then."""

    not_before: AwareDatetime
    note: Prose


class AssessResult(_Model):
    """An assess run's close: what it decided for the issue, where each done-when item
    stands, and the rubric. What follows is on the board already (the `work` item, the
    operator's items) or is written by the board from this close (the dated `assess` item
    of a `wait`)."""

    kind: Literal["assess"] = "assess"
    outcome: AssessOutcome
    # The reason, in a line.
    summary: Prose
    # A row per done-when item. None for an issue that names no done-when.
    done_when: tuple[DoneWhen, ...] = Field(default=(), max_length=30)
    # Every check of the rubric, once each.
    rubric: tuple[RubricChecked, ...] = Field(
        min_length=len(RubricCheck), max_length=len(RubricCheck)
    )
    # For `work` only: the `work` item the run wrote.
    work: ItemRef | None = None
    # The operator's items the run wrote. Needed for `operator` and `plan`.
    items: tuple[ItemRef, ...] = Field(default=(), max_length=10)
    # For `wait` only: the board writes the issue's next `assess` item from it.
    wait: Wait | None = None
    # For `close` only: the comment the issue was closed with.
    comment: CommentId | None = None

    @model_validator(mode="after")
    def _fields_fit_the_outcome(self) -> Self:
        named = [checked.check for checked in self.rubric]
        if set(named) != set(RubricCheck):
            raise ValueError(
                "`rubric` has every check once: " + ", ".join(c.value for c in RubricCheck)
            )
        if len(set(self.items)) != len(self.items):
            raise ValueError("an item is named twice")
        outcome = self.outcome
        for name, wanted in (
            ("work", AssessOutcome.WORK),
            ("wait", AssessOutcome.WAIT),
            ("comment", AssessOutcome.CLOSE),
        ):
            given = getattr(self, name) is not None
            if given and outcome is not wanted:
                raise ValueError(f"`{name}` is for the outcome {wanted.value} only")
            if not given and outcome is wanted:
                raise ValueError(f"{wanted.value} needs `{name}`")
        states = [row.state for row in self.done_when]
        if outcome is AssessOutcome.WORK:
            failed = [c.check.value for c in self.rubric if c.outcome is CheckOutcome.FAIL]
            if failed:
                raise ValueError(f"work needs a rubric with no failed check: {', '.join(failed)}")
        if outcome in (AssessOutcome.OPERATOR, AssessOutcome.PLAN) and not self.items:
            raise ValueError(f"{outcome.value} needs `items`: the items written for the operator")
        if outcome is AssessOutcome.NONE and self.items:
            raise ValueError("none writes nothing: leave `items` out")
        if outcome is AssessOutcome.WAIT and DoneWhenState.CANNOT_JUDGE not in states:
            raise ValueError("wait needs a done-when item that can't be judged yet")
        if outcome is AssessOutcome.CLOSE and (
            not states or any(state is not DoneWhenState.MET for state in states)
        ):
            raise ValueError("close needs every done-when item met, and at least one")
        return self


# Not discriminated by `kind`: a run's close may leave `kind` out. Each model forbids a
# field it does not know and needs one the others lack, so one fits at most. close_my_item
# then holds the result to the run's own stage.
StageResult = DeployCheckResult | ReviewResult | AssessResult

# A stage's result model. A stage that is not here has no run's close yet.
STAGE_RESULTS: dict[
    str, type[DeployCheckResult] | type[ReviewResult] | type[AssessResult]
] = {
    StageKind.DEPLOY_CHECK.value: DeployCheckResult,
    StageKind.REVIEW.value: ReviewResult,
    StageKind.ASSESS.value: AssessResult,
}

# The operator kinds a stage's run may write. A stage that is not here writes none. An
# assess run writes every kind but the two the reconciler writes from a PR's facts.
RUN_CREATES: dict[str, frozenset[str]] = {
    StageKind.DEPLOY_CHECK.value: frozenset({OperatorKind.DECISION.value}),
    StageKind.REVIEW.value: frozenset({OperatorKind.DECISION.value}),
    StageKind.ASSESS.value: frozenset(
        kind.value
        for kind in OperatorKind
        if kind not in (OperatorKind.PR_APPROVE, OperatorKind.PR_MERGE)
    ),
}

# The reasons a stage's run may give its own claim back with. The other reasons are the
# dispatcher's: it knows why a run was interrupted.
RUN_RELEASES: dict[str, frozenset[ReleaseReason]] = {
    StageKind.DEPLOY_CHECK.value: frozenset({ReleaseReason.NOT_LIVE}),
}


def parse_operator_item(data: object) -> OperatorItem:
    """Validate an operator item, picking the model from its `kind`.

    Raises pydantic.ValidationError when the kind is unknown or a field is refused.
    """
    return _OPERATOR_ITEM.validate_python(data)


def parse_stage_item(data: object) -> StageItem:
    """Validate a stage item, picking the model from its `kind`.

    Raises pydantic.ValidationError when the kind has no model or a field is refused.
    """
    return _STAGE_ITEM.validate_python(data)
