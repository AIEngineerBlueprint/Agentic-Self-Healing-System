"""Service catalog and policy loader.

Ownership, dependencies, contracts, and autonomy levels come from
platform/catalog/services.yaml; guardrails from platform/policy/policy.yaml.
Both are read at startup and cached, with published OpenAPI documents fetched
lazily so a service that is briefly down does not break attribution outright.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import pathlib
import threading
import time
from urllib.parse import urlsplit

import httpx
import yaml

log = logging.getLogger(__name__)

CATALOG_PATH = pathlib.Path(os.environ.get("CATALOG_PATH", "/app/catalog/services.yaml"))
POLICY_PATH = pathlib.Path(os.environ.get("POLICY_PATH", "/app/policy/policy.yaml"))

_catalog: dict = {}
_policy: dict = {}
_contracts: dict[str, dict] = {}
_contract_fetched_at: dict[str, float] = {}
_lock = threading.Lock()

CONTRACT_TTL_SECONDS = 60


def load() -> None:
    global _catalog, _policy
    _catalog = yaml.safe_load(CATALOG_PATH.read_text())
    _policy = yaml.safe_load(POLICY_PATH.read_text())
    log.info(
        "catalog loaded: %d services, %d repair agents",
        len(_catalog.get("services", {})),
        len(_catalog.get("repair_agents", {})),
    )


def services() -> dict:
    return _catalog.get("services", {})


def service(name: str) -> dict:
    return services().get(name, {})


def policy() -> dict:
    return _policy


def repository_for(service_name: str) -> str | None:
    return service(service_name).get("repository")


def owner_for(service_name: str) -> dict:
    owner_key = service(service_name).get("owner")
    return {"key": owner_key, **_catalog.get("owners", {}).get(owner_key, {})}


def autonomy_level(service_name: str) -> str:
    return service(service_name).get("autonomy_level", "L0")


def confidence_floors(repository: str | None = None) -> dict:
    """The two thresholds routing runs on, most specific source wins.

        1. services.yaml   confidence_floors: on the service owning `repository`
        2. environment     ASHS_AUTONOMOUS_FLOOR / ASHS_REVIEW_FLOOR  (from .env)
        3. policy.yaml     confidence: autonomous_floor / review_floor
        4. built-in        0.85 / 0.60

    Per-service is the one that earns its keep: "act autonomously on the product
    at 0.85, but demand 0.95 before touching emission factors" is a real
    ownership decision, and expressing it globally forces the strictest service
    to set the bar for everyone. The env layer exists so a floor can be moved
    for one demo without editing a mounted config.
    """
    floors = policy().get("confidence", {}) or {}
    autonomous = float(floors.get("autonomous_floor", 0.85))
    review = float(floors.get("review_floor", 0.60))

    for key, name in (("ASHS_AUTONOMOUS_FLOOR", "autonomous"),
                      ("ASHS_REVIEW_FLOOR", "review")):
        raw = os.environ.get(key)
        if raw:
            try:
                value = float(raw)
            except ValueError:
                log.warning("%s=%r is not a number; ignoring", key, raw)
                continue
            if name == "autonomous":
                autonomous = value
            else:
                review = value

    service_name = service_for_repository(repository or "") if repository else None
    override = (service(service_name or "").get("confidence_floors") or {})
    autonomous = float(override.get("autonomous", autonomous))
    review = float(override.get("review", review))

    if review > autonomous:
        log.warning("review floor %.2f exceeds autonomous floor %.2f for %s; "
                    "clamping review down", review, autonomous, repository)
        review = autonomous

    return {"autonomous": autonomous, "review": review,
            "source": "service" if override else "policy/env"}


def base_url(service_name: str) -> str | None:
    """Where this service actually lives.

    Explicit `base_url` wins; otherwise it is derived from the origin of
    `contract_url`, so a catalog entry that already publishes a contract does
    not have to repeat the host. Returns None when the service declares
    neither -- callers must treat that as "not reachable from here" rather
    than falling back to a guess.
    """
    spec = service(service_name)
    if spec.get("base_url"):
        return str(spec["base_url"]).rstrip("/")
    contract_url = spec.get("contract_url")
    if contract_url:
        parts = urlsplit(str(contract_url))
        if parts.scheme and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}"
    return None


def external_base_url(service_name: str) -> str | None:
    """The address a human outside the mesh can reach, falling back to the
    in-mesh one. Only used for things people read and run themselves."""
    spec = service(service_name)
    if spec.get("external_base_url"):
        return str(spec["external_base_url"]).rstrip("/")
    return base_url(service_name)


def health_url(service_name: str) -> str | None:
    """Liveness endpoint. Services differ (/health vs /api/health), so it is
    declared per service rather than assumed."""
    root = base_url(service_name)
    if not root:
        return None
    return root + (service(service_name).get("health_path") or "/health")


def monitored_services() -> list[str]:
    """Every service the control plane is responsible for.

    The whole catalog, not one product. What each service gets is decided by
    its own autonomy_level -- there is no separate 'in scope' list to drift.
    """
    return list(services().keys())


def service_for_repository(repository: str) -> str | None:
    """The deployable service that owns a repository's runtime.

    Attribution decides a REPOSITORY; execution needs the SERVICE to redeploy,
    health-check and replay against.
    """
    for name, spec in services().items():
        if spec.get("repository") == repository and spec.get("kind") in ("bff", "capability"):
            return name
    return None


def source_layout(service_name: str) -> dict:
    return service(service_name).get("source_layout") or {}


def test_env(service_name: str) -> dict:
    """Environment handed to the agent's generated tests, so a test can find
    the service it is testing without the URL being compiled into the gate."""
    spec = service(service_name)
    var, root = spec.get("test_env_var"), base_url(service_name)
    return {var: root} if var and root else {}


def validation_spec(service_name: str) -> dict:
    return service(service_name).get("validation") or {}


def consumers_of(service_name: str) -> list[str]:
    """Which services depend on this one. Drives the blast-radius check."""
    return [
        name
        for name, spec in services().items()
        if service_name in (spec.get("depends_on") or [])
    ]


def repair_agent_for(repository: str) -> str | None:
    for agent, spec in _catalog.get("repair_agents", {}).items():
        if repository in (spec.get("handles_repositories") or []):
            return agent
    return None


def _is_canonical(path: str) -> bool:
    """A repo-relative path with no `..`, `.`, empty segment or leading slash.

    Both guards below glob-match the path AS WRITTEN. A model-supplied
    `cfc-product/api/src/../../../cap-factors/data/seed.sql` matches the
    product agent's `cfc-product/api/src/**` allowlist and misses the
    `cap-factors/data/**` protected pattern, yet resolves to the protected
    file. Refusing non-canonical paths outright closes that gap.
    """
    return bool(path) and not path.startswith("/") and all(
        part not in ("", ".", "..") for part in path.split("/"))


def agent_may_write(agent: str, path: str) -> bool:
    """Path allowlist. Enforced in the tool gateway, never by prompt."""
    if not _is_canonical(path):
        return False
    spec = _catalog.get("repair_agents", {}).get(agent, {})
    return any(fnmatch.fnmatch(path, p) for p in spec.get("write_paths") or [])


def is_protected(path: str) -> bool:
    """Protected zone check. Always human, whatever the confidence.

    Fails closed: a path that is not canonical is treated as protected.
    """
    if not _is_canonical(path):
        return True
    return any(fnmatch.fnmatch(path, p) for p in _policy.get("protected_paths") or [])


def sensitive_zone_for(path: str) -> dict | None:
    for zone in _policy.get("sensitive_zones") or []:
        if any(fnmatch.fnmatch(path, p) for p in zone.get("paths") or []):
            return zone
    return None


def contract(service_name: str) -> dict | None:
    """Fetch a service's published OpenAPI document, cached briefly.

    Generated from code, never hand-maintained -- a stale hand-written spec
    would silently break the entire attribution model.
    """
    url = service(service_name).get("contract_url")
    if not url:
        return None

    with _lock:
        fetched = _contract_fetched_at.get(service_name, 0)
        if service_name in _contracts and (time.time() - fetched) < CONTRACT_TTL_SECONDS:
            return _contracts[service_name]

    try:
        with httpx.Client(timeout=5.0) as client:
            response = client.get(url)
        if response.status_code == 200:
            doc = response.json()
            with _lock:
                _contracts[service_name] = doc
                _contract_fetched_at[service_name] = time.time()
            return doc
        log.warning("contract fetch for %s returned %s", service_name, response.status_code)
    except httpx.RequestError as exc:
        log.warning("contract fetch for %s failed: %s", service_name, exc)

    with _lock:
        return _contracts.get(service_name)


def response_schema(service_name: str, operation: str, status_code: int = 200) -> dict | None:
    """Resolve the JSON schema a given operation's response must satisfy.

    FastAPI emits internal references as `#/components/schemas/X`. Those are
    resolved against the ROOT of whatever schema document the validator is
    given, so the extracted schema carries a `components.schemas` block rather
    than a `$defs` one -- otherwise every `$ref` dangles and validation raises
    PointerToNowhere instead of returning a verdict.
    """
    doc = contract(service_name)
    if not doc:
        return None

    spec = service(service_name).get("operations", {}).get(operation)
    schemas = doc.get("components", {}).get("schemas", {})

    if spec:
        key = "success_schema" if 200 <= status_code < 300 else "error_schema"
        name = spec.get(key)
        if name and name in schemas:
            return {**schemas[name], "components": {"schemas": schemas}}

    # Fall back to reading the path item straight out of the document.
    try:
        method, path = operation.split(" ", 1)
        node = doc["paths"][path][method.lower()]["responses"][str(status_code)]
        ref = node["content"]["application/json"]["schema"]
        if "$ref" in ref:
            name = ref["$ref"].rsplit("/", 1)[-1]
            return {**schemas[name], "components": {"schemas": schemas}}
        return {**ref, "components": {"schemas": schemas}}
    except (KeyError, ValueError, TypeError):
        return None


def journeys() -> list[dict]:
    """Every declared entry point, from services.yaml.

    The dashboard previously monitored ONE hardcoded endpoint while calling the
    panel "journeys" -- which overclaims and, worse, hides the other entry
    points a product actually exposes. `entry_points` already existed in the
    catalog and simply was not read.
    """
    products = _catalog.get("products", {})
    out = []
    for name, spec in services().items():
        stack = spec.get("stack")
        for ep in spec.get("entry_points") or []:
            # Accept a bare path or a {name, path} pair, so adding a friendly
            # name never breaks an existing catalog.
            path = ep if isinstance(ep, str) else ep.get("path")
            label = path if isinstance(ep, str) else (ep.get("name") or path)
            out.append({
                "service": name,
                "service_name": spec.get("display_name") or name,
                "product": products.get(stack, {}).get("name") or stack,
                "repository": spec.get("repository"),
                "stack": stack,
                "journey": label,
                "entry_point": path,
            })
    return out
