"""
l0_generator.py
Statically generates the L0 project map.  No LLM required.

The project root (cfg.project_root()) is already the scoped directory from
root_path — no separate scope_path filtering needed.  Entry points, modules,
and routing files are all discovered relative to that root.

Output is stored in l0_maps as JSONB with a "raw_text" key for direct MCP injection.
"""
import json, re, logging
from pathlib import Path
import config_loader as cfg
import db

log = logging.getLogger(__name__)


# ── Kotlin / KMP helpers ──────────────────────────────────────────────────────

def _parse_gradle_modules(root: Path) -> list[str]:
    for fname in ("settings.gradle.kts", "settings.gradle"):
        f = root / fname
        if f.exists():
            return re.findall(r'include\s*\(\s*["\']([^"\']+)["\']', f.read_text(errors="replace"))
    return []


def _parse_gradle_deps(root: Path, modules: list[str]) -> dict:
    deps = {}
    for mod in modules:
        mod_path = root / mod.lstrip(":").replace(":", "/")
        for fname in ("build.gradle.kts", "build.gradle"):
            f = mod_path / fname
            if f.exists():
                found = re.findall(r'project\s*\(\s*["\']([^"\']+)["\']', f.read_text(errors="replace"))
                if found:
                    deps[mod] = found
    return deps


def _find_entry_points_kotlin(root: Path) -> list[dict]:
    entries = []
    for p in root.rglob("*.kt"):
        try:
            text = p.read_text(errors="replace")
        except Exception:
            continue
        if "fun main(" in text or "embeddedServer(" in text or "Application.module" in text:
            kind = "ktor-entry" if "embeddedServer(" in text else "main"
            entries.append({"path": str(p.relative_to(root)), "kind": kind})
    return entries[:10]


def _find_routing_files_kotlin(root: Path) -> list[str]:
    results = []
    for p in root.rglob("*.kt"):
        try:
            text = p.read_text(errors="replace")
        except Exception:
            continue
        if "routing {" in text or "route(" in text:
            results.append(str(p.relative_to(root)))
    return results[:15]


# ── Java / Maven helpers ──────────────────────────────────────────────────────

def _parse_maven_modules(root: Path) -> list[str]:
    """Parse Maven multi-module project from the root pom.xml."""
    pom = root / "pom.xml"
    if not pom.exists():
        return []
    try:
        text = pom.read_text(errors="replace")
        return re.findall(r"<module>\s*([^<\s]+)\s*</module>", text)
    except Exception:
        return []


def _find_entry_points_java(root: Path) -> list[dict]:
    """Find Spring Boot application entry points and plain main() classes."""
    entries = []
    for p in root.rglob("*.java"):
        try:
            text = p.read_text(errors="replace")
        except Exception:
            continue
        if "@SpringBootApplication" in text:
            entries.append({"path": str(p.relative_to(root)), "kind": "spring-boot-entry"})
        elif re.search(r"public\s+static\s+void\s+main\s*\(", text):
            entries.append({"path": str(p.relative_to(root)), "kind": "main"})
    return entries[:10]


def _find_routing_files_java(root: Path) -> list[str]:
    """Find Spring MVC/WebFlux controller files."""
    results = []
    for p in root.rglob("*.java"):
        try:
            text = p.read_text(errors="replace")
        except Exception:
            continue
        if "@RestController" in text or "@Controller" in text or "@RequestMapping" in text:
            results.append(str(p.relative_to(root)))
    return results[:15]


# ── TypeScript / React / Angular / Vue helpers ────────────────────────────────

def _parse_package_json_workspaces(root: Path) -> list[str]:
    pkg = root / "package.json"
    if not pkg.exists():
        return []
    try:
        data = json.loads(pkg.read_text())
        ws = data.get("workspaces", [])
        if isinstance(ws, dict):
            ws = ws.get("packages", [])
        return ws
    except Exception:
        return []


def _find_entry_points_ts(root: Path) -> list[dict]:
    entries = []
    patterns = {"main.ts", "main.tsx", "App.tsx", "app.module.ts",
                "index.ts", "index.tsx", "server.ts",
                "main.js", "App.vue"}    # App.vue is the Vue root component
    for p in root.rglob("*"):
        if p.name in patterns and "node_modules" not in str(p):
            entries.append({"path": str(p.relative_to(root)), "kind": p.stem})
    return entries[:10]


# ── L0 text renderer ──────────────────────────────────────────────────────────

def _profile_summary(profile: dict) -> str:
    """Short one-line summary for logs/L0 header, e.g. 'typescript/frontend, python/backend'."""
    parts = []
    for c in profile.get("components", []):
        part = f"{c['language']}/{c['layer']}"
        if c.get("framework"):
            part += f":{c['framework']}"
        parts.append(part)
    return ", ".join(parts) or "unknown"


def _render_l0(project_key: str, profile: dict, root_path: str,
               modules: list, entry_points: list, deps: dict,
               routing: list, workspaces: list,
               routing_label: str = "## Routing Files",
               maven_modules: list | None = None) -> str:
    lines = [f"# L0 Project Map: {project_key}", f"Profile: {_profile_summary(profile)}"]
    if root_path:
        lines.append(f"Root: {root_path}")
    lines.append("")

    if modules:
        lines.append("## Gradle Modules")
        for m in modules:
            dep_list = deps.get(m, [])
            dep_str = f" → depends on: {', '.join(dep_list)}" if dep_list else ""
            lines.append(f"  {m}{dep_str}")
        lines.append("")

    if maven_modules:
        lines.append("## Maven Modules")
        for m in maven_modules:
            lines.append(f"  {m}")
        lines.append("")

    if workspaces:
        lines.append("## NPM Workspaces")
        for ws in workspaces:
            lines.append(f"  {ws}")
        lines.append("")

    if entry_points:
        lines.append("## Entry Points")
        for ep in entry_points:
            lines.append(f"  [{ep['kind']}] {ep['path']}")
        lines.append("")

    if routing:
        lines.append(routing_label)
        for r in routing:
            lines.append(f"  {r}")
        lines.append("")

    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────

def run_l0() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    root      = Path(cfg.project_root())
    pkey      = cfg.project_key()
    p_profile = cfg.profile()
    root_path = cfg.get_project().get("root_path", "")
    components = p_profile.get("components", [])

    log.info(f"Generating L0 for {pkey} (profile: {_profile_summary(p_profile)}, root: '{root}')")

    modules, deps, entry_points, routing, workspaces, maven_modules = [], {}, [], [], [], []
    routing_label = "## Routing Files"

    languages = {c["language"] for c in components}
    layers = {c["layer"] for c in components}

    if "kotlin" in languages:
        modules      = _parse_gradle_modules(root)
        deps         = _parse_gradle_deps(root, modules)
        entry_points = entry_points + _find_entry_points_kotlin(root)
        routing      = routing + _find_routing_files_kotlin(root)
        routing_label = "## Ktor Routing Files"

    if "java" in languages:
        maven_modules = _parse_maven_modules(root)
        # Also check for Gradle-based Java projects
        if not maven_modules:
            modules = modules or _parse_gradle_modules(root)
            deps    = deps    or _parse_gradle_deps(root, modules)
        entry_points  = entry_points + _find_entry_points_java(root)
        routing       = routing + _find_routing_files_java(root)
        routing_label = "## Controllers"

    if "frontend" in layers:
        workspaces   = _parse_package_json_workspaces(root)
        entry_points += _find_entry_points_ts(root)

    # A monorepo mixing a Kotlin/Java backend with a frontend already picks
    # up both branches above (each keyed on its own component's language),
    # so no separate "multiplatform"/"mixed" special-case is needed anymore
    # — that used to exist only because the old flat profile string
    # couldn't represent "both" without a third catch-all value.

    raw_text = _render_l0(pkey, p_profile, root_path, modules, entry_points, deps,
                          routing, workspaces, routing_label, maven_modules)

    map_json = {
        "raw_text":        raw_text,
        "modules":         modules,
        "maven_modules":   maven_modules,
        "entry_points":    entry_points,
        "dependency_graph": deps,
        "routing_files":   routing,
        "workspaces":      workspaces,
    }

    db.upsert_l0(pkey, map_json)
    log.info(f"L0 stored ({len(raw_text)} chars):\n{raw_text}")
