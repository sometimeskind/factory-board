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
import unicodedata
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
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


OPERATOR_KINDS: tuple[str, ...] = tuple(kind.value for kind in OperatorKind)
STAGE_KINDS: tuple[str, ...] = tuple(kind.value for kind in StageKind)
KINDS: tuple[str, ...] = OPERATOR_KINDS + STAGE_KINDS
MACHINES: tuple[str, ...] = tuple(machine.value for machine in Machine)
SUBJECT_TYPES: tuple[str, ...] = tuple(subject_type.value for subject_type in SubjectType)
TERRAFORM_MODULES: tuple[str, ...] = tuple(module.value for module in TerraformModule)
KUBECTL_VERBS: tuple[str, ...] = tuple(verb.value for verb in KubectlVerb)


class ReleaseReason(StrEnum):
    """Why a run's claim was given back: a known interruption, which is not an expiry."""

    # The run's unit did not start.
    START_FAILED = "start-failed"
    INFRA_WINDOW = "infra-window"
    USAGE_LIMIT = "usage-limit"
    # A clean stop: systemctl stop, or pilot shutting down.
    STOPPED = "stopped"
    # A deploy-check looked, and the change is not live yet.
    NOT_LIVE = "not-live"


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
    kind: Literal["assess"] = "assess"
    issue: Ticket
    # On a dated item: what to check once the date is reached.
    note: Prose | None = None

    @property
    def subject(self) -> Subject:
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
    # issue closes, the board wakes assess for it; a check that names none wakes nothing.
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
