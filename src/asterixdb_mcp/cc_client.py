"""Thin async HTTP client for the AsterixDB Cluster Controller REST API.

This is a templating forwarder: it builds form parameters, applies the egress
ceilings, posts to the CC, and parses the JSON envelope. It never parses SQL++
or inspects statement semantics; that's the CC's job.

Two invariants are enforced here rather than left to callers. First, readonly=true
is hardcoded on every /query/service call, the single mutation guard (the CC
rejects any non-query statement). Second, the response body is checked against
the gateway byte ceiling before parsing.

The client takes an injected httpx.AsyncClient, so tests can drive it with
httpx.MockTransport and assert on the exact form parameters sent.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from .config import Settings
from .egress import enforce_byte_ceiling, format_timeout
from .errors import ErrorType, GatewayError, classify_cc_error

QUERY_SERVICE_PATH = "/query/service"
ADMIN_VERSION_PATH = "/admin/version"
ADMIN_CLUSTER_PATH = "/admin/cluster"
ADMIN_CLUSTER_NODE_PATH = "/admin/cluster/node"
ADMIN_DIAGNOSTICS_PATH = "/admin/diagnostics"
ADMIN_RUNNING_REQUESTS_PATH = "/admin/requests/running"

# Envelope fields, verified against the CC response printers.
HANDLE_FIELD = "handle"
STATUS_FIELD = "status"

# The async-result plan format that yields a JSON operator tree (clean_json).
PLAN_FORMAT_JSON = "clean_json"

# Acceptable JSON values that the CC may use for a successful query envelope.
_SUCCESS_STATUSES = frozenset({"success"})


class CCClient:
    """Async wrapper over the AsterixDB CC REST endpoints used by the gateway tools."""

    def __init__(self, settings: Settings, http: httpx.AsyncClient) -> None:
        self._settings = settings
        self._http = http

    # /query/service

    async def execute(
        self,
        statement: str,
        *,
        client_context_id: str,
        dataverse: str | None = None,
        signature: bool = False,
        profile: bool = False,
        max_warnings: int = 5,
        compiler_parameters: dict[str, Any] | None = None,
        statement_parameters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run a synchronous, read-only SQL++ statement and return the CC envelope.

        statement_parameters bind SQL++ named parameters ($name) so callers never
        splice user input into the statement text.

        Raises:
            GatewayError: classified failure (readonly violation, timeout, syntax,
                size limit, or internal).
        """
        form = self._build_query_form(
            statement,
            client_context_id=client_context_id,
            dataverse=dataverse,
            signature=signature,
            profile=profile,
            max_warnings=max_warnings,
            compiler_parameters=compiler_parameters,
            statement_parameters=statement_parameters,
        )
        envelope = await self._post_query(form)
        self._raise_on_envelope_error(envelope)
        return envelope

    async def submit_async(
        self,
        statement: str,
        *,
        client_context_id: str,
        dataverse: str | None = None,
        compiler_parameters: dict[str, Any] | None = None,
        statement_parameters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Submit a read-only statement in ``mode=async`` and return the CC envelope.

        The CC compiles the statement synchronously, then runs it in the
        background and returns a status handle. A compilation/parse failure is
        reported inline (``errors`` in the envelope) and raised here; a
        successfully submitted job returns with a ``handle`` and a running status.
        """
        form = self._build_query_form(
            statement,
            client_context_id=client_context_id,
            dataverse=dataverse,
            signature=False,
            profile=False,
            max_warnings=5,
            compiler_parameters=compiler_parameters,
            statement_parameters=statement_parameters,
            mode="async",
        )
        envelope = await self._post_query(form)
        # A submit-time failure (syntax/semantic) carries errors even though the
        # job never started; surface it. A successfully submitted job reports a
        # non-success status ("running"), which is expected here and not an error.
        self._raise_on_envelope_errors(envelope)
        return envelope

    async def compile_query(
        self,
        statement: str,
        *,
        client_context_id: str,
        dataverse: str | None = None,
        emit_plan: bool = False,
        signature: bool = False,
        statement_parameters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Compile a statement without running it (``compile-only=true``).

        Returns the raw CC envelope WITHOUT raising on a compilation error: the
        caller (validate_syntax / explain_query) inspects ``errors`` to classify
        the failure as data rather than as a gateway error. Transport and size
        failures still raise.

        When ``emit_plan`` is set, the optimized logical plan is requested as a
        JSON operator tree (``plans.optimizedLogicalPlan``).
        """
        form = self._build_query_form(
            statement,
            client_context_id=client_context_id,
            dataverse=dataverse,
            signature=signature,
            profile=False,
            max_warnings=5,
            compiler_parameters=None,
            statement_parameters=statement_parameters,
            compile_only=True,
            emit_plan=emit_plan,
        )
        return await self._post_query(form)

    async def poll_status(self, handle: str) -> dict[str, Any]:
        """GET an async status handle and return its envelope (status, result handle)."""
        return await self._get_json(handle)

    async def fetch_result(self, handle: str) -> dict[str, Any]:
        """GET an async result handle and return its envelope (results, metrics)."""
        return await self._get_json(handle)

    async def cancel(self, client_context_id: str) -> bool:
        """Cancel a running request by its clientContextID.

        Returns True when the CC reports the request was cancelled. Raises
        GatewayError(NOT_FOUND) when no running request matches, FORBIDDEN when
        the request exists but cannot be cancelled.
        """
        try:
            response = await self._http.request(
                "DELETE",
                ADMIN_RUNNING_REQUESTS_PATH,
                params={"client_context_id": client_context_id},
                headers=self._headers(),
            )
        except httpx.TimeoutException as exc:
            raise GatewayError(ErrorType.TIMEOUT, "Cancel request timed out.") from exc
        except httpx.HTTPError as exc:
            raise GatewayError(
                ErrorType.INTERNAL, f"Failed to reach AsterixDB to cancel request: {exc}"
            ) from exc

        if response.status_code == httpx.codes.OK:
            return True
        if response.status_code == httpx.codes.NOT_FOUND:
            raise GatewayError(
                ErrorType.NOT_FOUND,
                f"No running request found for clientContextID {client_context_id!r}. "
                "It may have already finished or been cancelled.",
            )
        if response.status_code == httpx.codes.FORBIDDEN:
            raise GatewayError(
                ErrorType.FORBIDDEN,
                f"The request for clientContextID {client_context_id!r} exists but "
                "cannot be cancelled.",
            )
        raise GatewayError(
            ErrorType.INTERNAL,
            f"Cancel returned unexpected status {response.status_code}.",
        )

    def _build_query_form(
        self,
        statement: str,
        *,
        client_context_id: str,
        dataverse: str | None,
        signature: bool,
        profile: bool,
        max_warnings: int,
        compiler_parameters: dict[str, Any] | None,
        statement_parameters: dict[str, Any] | None,
        mode: str | None = None,
        compile_only: bool = False,
        emit_plan: bool = False,
    ) -> dict[str, str]:
        """Assemble the /query/service form parameters.

        readonly and timeout are gateway-enforced and can't be overridden by a
        caller. They're set here last, so nothing in compiler_parameters can
        shadow them.
        """
        form: dict[str, str] = {
            "statement": statement,
            "client_context_id": client_context_id,
            "signature": _bool_param(signature),
            "max-warnings": str(max_warnings),
        }
        if dataverse:
            form["dataverse"] = dataverse
        if profile:
            # The CC profile parameter is an enum; "timings" yields the metrics
            # block the analyze_query_performance prompt consumes.
            form["profile"] = "timings"
        if mode:
            form["mode"] = mode
        if compile_only:
            form["compile-only"] = "true"
        if emit_plan:
            form["optimized-logical-plan"] = "true"
            form["plan-format"] = PLAN_FORMAT_JSON
        if compiler_parameters:
            # Tuning knobs are validated against the allowlist in the tool layer
            # before they get here; forward them verbatim.
            for key, value in compiler_parameters.items():
                form[key] = _scalar_param(value)
        if statement_parameters:
            for name, value in statement_parameters.items():
                # SQL++ named statement parameters: the CC reads a request field
                # named "$<name>" whose value is the JSON-encoded literal. The
                # engine binds it; it is never spliced into the statement text.
                form[f"${name}"] = json.dumps(value)

        # Gateway-enforced, written last so they win over any collision above.
        form["readonly"] = "true"
        form["timeout"] = format_timeout(self._settings.max_time_ms)
        return form

    async def _post_query(self, form: dict[str, str]) -> dict[str, Any]:
        """POST the form to ``/query/service``, enforce the byte ceiling, parse JSON."""
        try:
            response = await self._http.post(
                QUERY_SERVICE_PATH,
                data=form,
                headers=self._headers(),
            )
        except httpx.TimeoutException as exc:
            raise GatewayError(
                ErrorType.TIMEOUT,
                f"AsterixDB did not respond within {self._settings.request_timeout_s}s.",
            ) from exc
        except httpx.HTTPError as exc:
            raise GatewayError(
                ErrorType.INTERNAL,
                f"Failed to reach AsterixDB at {self._settings.cc_base_url}: {exc}",
            ) from exc

        body = enforce_byte_ceiling(response.content, self._settings.max_bytes_per_query)
        return _parse_json_body(body)

    @classmethod
    def _raise_on_envelope_error(cls, envelope: dict[str, Any]) -> None:
        """Inspect the CC envelope; raise a classified error if it reports failure.

        Used by the synchronous path, where any non-success status is a failure.
        """
        cls._raise_on_envelope_errors(envelope)
        status = str(envelope.get("status", "")).lower()
        if status and status not in _SUCCESS_STATUSES:
            raise GatewayError(
                ErrorType.INTERNAL,
                f"AsterixDB returned non-success status {status!r} with no error detail.",
            )

    @staticmethod
    def _raise_on_envelope_errors(envelope: dict[str, Any]) -> None:
        """Raise a classified error only if the envelope carries an ``errors`` list.

        Used by the async submit path, where a running job legitimately reports a
        non-success status but no errors.
        """
        errors = envelope.get("errors")
        if errors:
            first = errors[0] if isinstance(errors, list) and errors else {}
            code = first.get("code") if isinstance(first, dict) else None
            message = (
                first.get("msg") if isinstance(first, dict) else None
            ) or "AsterixDB returned an unspecified error."
            raise classify_cc_error(asterix_code=code, message=message)

    # /admin endpoints

    async def admin_version(self) -> dict[str, Any]:
        """GET ``/admin/version``."""
        return await self._get_json(ADMIN_VERSION_PATH)

    async def admin_cluster(self) -> dict[str, Any]:
        """GET ``/admin/cluster``."""
        return await self._get_json(ADMIN_CLUSTER_PATH)

    async def admin_node_detail(self, node_id: str) -> dict[str, Any]:
        """GET ``/admin/cluster/node/{node_id}`` (per-NC stats)."""
        return await self._get_json(f"{ADMIN_CLUSTER_NODE_PATH}/{node_id}")

    async def admin_diagnostics(self) -> dict[str, Any]:
        """GET ``/admin/diagnostics`` (per-node heap, GC, threads, disk)."""
        return await self._get_json(ADMIN_DIAGNOSTICS_PATH)

    async def _get_json(self, path: str) -> dict[str, Any]:
        try:
            response = await self._http.get(path, headers=self._headers())
        except httpx.TimeoutException as exc:
            raise GatewayError(
                ErrorType.TIMEOUT, f"AsterixDB admin endpoint {path} timed out."
            ) from exc
        except httpx.HTTPError as exc:
            raise GatewayError(
                ErrorType.INTERNAL, f"Failed to reach AsterixDB admin endpoint {path}: {exc}"
            ) from exc
        body = enforce_byte_ceiling(response.content, self._settings.max_bytes_per_query)
        return _parse_json_body(body)

    # helpers

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._settings.cc_shared_secret:
            headers["X-Gateway-Secret"] = self._settings.cc_shared_secret
        return headers


def _bool_param(value: bool) -> str:
    return "true" if value else "false"


def _scalar_param(value: Any) -> str:
    """Render a compiler-parameter scalar to its form-field string."""
    if isinstance(value, bool):
        return _bool_param(value)
    return str(value)


def _parse_json_body(body: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise GatewayError(
            ErrorType.INTERNAL, f"AsterixDB returned a non-JSON response: {exc}"
        ) from exc
    if not isinstance(parsed, dict):
        raise GatewayError(
            ErrorType.INTERNAL,
            f"Expected a JSON object from AsterixDB, got {type(parsed).__name__}.",
        )
    return parsed
