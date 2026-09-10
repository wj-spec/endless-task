"""S8 生态检索与安装：把 skills.sh 生态的技能装进本地（经我们的安全门禁）。

设计（05 §2 Level 2 / §5）：

- **检索**：调用 ``npx skills find <query>``（只读、无落盘），解析出
  ``owner/repo@skill`` + 安装量 + 详情链接；安装量用于质量判断
  （参考 find-skills 的建议：优先 1K+ 安装量）。
- **安装**：**不**直接调用 ``npx skills add``（那会把技能装进
  ``~/.claude/skills`` 等共享目录，绕过我们的扫描门禁）。改为：
  下载 GitHub tarball 到临时 staging → 定位技能包目录 → 交给
  ``import_skill_package``（staging → 静态扫描 → 原子激活 → 可回滚）。
- 网络与执行都在服务端；调用方（API / 工具）负责审批与 provenance 记录。
"""

from __future__ import annotations

import io
import re
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence, Tuple

#: 生态检索超时（npx 首次可能要下载 CLI）。
SEARCH_TIMEOUT_SECONDS = 90.0
#: 下载超时。
DOWNLOAD_TIMEOUT_SECONDS = 120.0
#: 单次检索返回上限。
MAX_ECOSYSTEM_RESULTS = 10
#: tarball 解压后的最大文件数（防御 zip bomb / 超大仓）。
MAX_ARCHIVE_ENTRIES = 20_000

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
#: ``owner/repo@skill``（skill 名允许 ``:`` 与 ``/``，见生态里的 react:components）
_SPEC_RE = re.compile(
    r"^(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)"
    r"(?:@(?P<skill>[A-Za-z0-9_./:-]+))?$"
)
_INSTALLS_RE = re.compile(r"^(?P<spec>[^\s]+)\s+(?P<installs>[\d.]+[KMB]?)\s+installs$")
_URL_RE = re.compile(r"https://skills\.sh/\S+")


@dataclass(frozen=True)
class EcosystemHit:
    spec: str
    owner: str
    repo: str
    skill: str
    installs: str
    url: str

    def as_dict(self) -> dict[str, object]:
        return {
            "spec": self.spec,
            "owner": self.owner,
            "repo": self.repo,
            "skill": self.skill,
            "installs": self.installs,
            "url": self.url,
        }


@dataclass(frozen=True)
class EcosystemSearchResult:
    hits: Tuple[EcosystemHit, ...]
    error: str = ""


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text or "")


def parse_find_output(text: str, *, limit: int = MAX_ECOSYSTEM_RESULTS) -> Tuple[EcosystemHit, ...]:
    """解析 ``npx skills find`` 的终端输出（已去 ANSI 或未去都可）。"""
    lines = [line.strip() for line in strip_ansi(text).splitlines()]
    hits: list[EcosystemHit] = []
    pending: Optional[tuple[str, str]] = None
    for line in lines:
        if not line:
            continue
        match = _INSTALLS_RE.match(line)
        if match:
            spec = match.group("spec")
            pending = (spec, match.group("installs"))
            continue
        if pending is not None and line.startswith("└") or line.startswith("https://"):
            url_match = _URL_RE.search(line)
            spec, installs = pending
            parsed = _SPEC_RE.match(spec)
            if parsed is not None:
                hits.append(
                    EcosystemHit(
                        spec=spec,
                        owner=parsed.group("owner"),
                        repo=parsed.group("repo"),
                        skill=parsed.group("skill") or "",
                        installs=installs,
                        url=url_match.group(0) if url_match else "",
                    )
                )
            elif url_match is not None:
                hits.append(
                    EcosystemHit(
                        spec=spec,
                        owner="",
                        repo="",
                        skill="",
                        installs=installs,
                        url=url_match.group(0),
                    )
                )
            pending = None
            if len(hits) >= limit:
                break
    return tuple(hits)


def search_ecosystem(
    query: str,
    *,
    runner: Optional[Callable[[Sequence[str]], str]] = None,
    limit: int = MAX_ECOSYSTEM_RESULTS,
) -> EcosystemSearchResult:
    """检索生态（只读）。runner 可注入以便测试。"""
    cleaned = (query or "").strip()
    if not cleaned:
        return EcosystemSearchResult(hits=(), error="查询不能为空。")
    run = runner or _run_cli
    try:
        output = run(["npx", "--yes", "skills@latest", "find", cleaned])
    except Exception as error:  # noqa: BLE001 网络/CLI 失败都不是致命错误
        return EcosystemSearchResult(
            hits=(), error=f"生态检索失败：{type(error).__name__}。"
        )
    hits = parse_find_output(output, limit=limit)
    if not hits:
        return EcosystemSearchResult(hits=(), error="生态里没有匹配的技能。")
    return EcosystemSearchResult(hits=hits)


def _run_cli(argv: Sequence[str]) -> str:
    completed = subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        timeout=SEARCH_TIMEOUT_SECONDS,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"skills CLI 退出码 {completed.returncode}: "
            f"{strip_ansi(completed.stderr)[:200]}"
        )
    return completed.stdout


def validspec(source: str) -> Optional[re.Match]:
    """``owner/repo`` 或 ``owner/repo@skill``；拒绝任何路径/参数注入。"""
    text = (source or "").strip()
    if not text or text.startswith("-") or ".." in text or " " in text:
        return None
    return _SPEC_RE.match(text)


def download_skill_package(
    source: str,
    staging_root: Path,
    *,
    downloader: Optional[Callable[[str], bytes]] = None,
) -> Path:
    """把生态技能下载到 ``staging_root`` 并返回**技能包目录**（含 SKILL.md）。

    只支持 GitHub 上的技能仓（``owner/repo[@skill]``）；下载整仓 tarball 后
    在仓库里定位技能目录（``<skill>/SKILL.md``，找不到就退化为根 SKILL.md）。
    """
    parsed = validspec(source)
    if parsed is None:
        raise ValueError("来源格式应为 owner/repo 或 owner/repo@skill。")
    owner = parsed.group("owner")
    repo = parsed.group("repo")
    skill = parsed.group("skill") or ""
    fetch = downloader or _download_tarball
    payload = fetch(f"https://codeload.github.com/{owner}/{repo}/tar.gz/HEAD")
    if not payload:
        raise ValueError("下载失败：仓库没有返回内容。")

    extract_root = Path(tempfile.mkdtemp(prefix="skill-fetch-", dir=str(staging_root)))
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            members = archive.getmembers()
            if len(members) > MAX_ARCHIVE_ENTRIES:
                raise ValueError("仓库文件过多，已拒绝解压。")
            for member in members:
                name = member.name
                if name.startswith("/") or ".." in Path(name).parts:
                    raise ValueError("压缩包包含越界路径，已拒绝。")
                if member.issym() or member.islnk():
                    # 符号链接不落盘（与技能扫描器的 symlink 策略一致）。
                    continue
            archive.extractall(extract_root)
        repo_root = _single_subdirectory(extract_root)
    except Exception:
        shutil.rmtree(extract_root, ignore_errors=True)
        raise

    package = _locate_package(repo_root, skill=skill)
    if package is None:
        candidates = describe_repo_skills(repo_root)
        shutil.rmtree(extract_root, ignore_errors=True)
        if candidates:
            raise ValueError(
                f"仓库 {owner}/{repo} 里没有「{skill}」这个技能；"
                f"可用技能：{'、'.join(candidates[:12])}"
                f"{'…' if len(candidates) > 12 else ''}。"
                "请用 owner/repo@<技能名> 重新指定。"
            )
        raise ValueError(
            f"仓库 {owner}/{repo} 里没有找到技能包（没有 SKILL.md）。"
        )
    return package


def _download_tarball(url: str) -> bytes:
    """下载 tarball；网络错误统一转成 ValueError（调用方按 400 处理，不 500）。"""
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, headers={"User-Agent": "endless-task"})
    try:
        with urllib.request.urlopen(  # noqa: S310 固定 https + 受控主机
            request, timeout=DOWNLOAD_TIMEOUT_SECONDS
        ) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        raise ValueError(
            f"下载失败：GitHub 返回 {error.code}（检查 owner/repo 是否存在）。"
        ) from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise ValueError(f"下载失败：{type(error).__name__}。") from error


def _single_subdirectory(root: Path) -> Path:
    entries = [item for item in root.iterdir() if item.is_dir()]
    if len(entries) == 1:
        return entries[0]
    return root


#: 定位技能包时要跳过的目录（依赖/VCS/回收站）。
_SKIP_DIRECTORIES = frozenset(
    {".git", ".hg", ".svn", "node_modules", ".trash", "__pycache__", ".venv"}
)


def _normalize_label(value: str) -> str:
    """技能名比较用的宽松标签（去掉 ``:`` ``/`` ``_`` ``-`` 并小写）。"""
    return re.sub(r"[:/_\-\s]", "", value).lower()


def _iter_skill_files(repo_root: Path) -> list[Path]:
    """仓库里所有 ``SKILL.md``（跳过依赖目录与隐藏目录）。"""
    found: list[Path] = []
    for path in repo_root.rglob("SKILL.md"):
        parts = path.relative_to(repo_root).parts[:-1]
        if any(part in _SKIP_DIRECTORIES or part.startswith(".") for part in parts):
            continue
        found.append(path)
    return sorted(found)


def _frontmatter_name(skill_file: Path) -> str:
    """尽力读出 frontmatter 里的 ``name``（读不出来返回空串）。"""
    try:
        text = skill_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    try:
        raw, _body = _split_frontmatter(text)
    except ValueError:
        return ""
    return str(raw.get("name") or "").strip()


def describe_repo_skills(repo_root: Path) -> list[str]:
    """仓库里可安装的技能名（优先 frontmatter name，退化用目录名）。"""
    names: list[str] = []
    for skill_file in _iter_skill_files(repo_root):
        names.append(_frontmatter_name(skill_file) or skill_file.parent.name)
    return names


def _locate_package(repo_root: Path, *, skill: str) -> Optional[Path]:
    """在仓库里定位技能包目录。

    生态里的 ``@<skill>`` 是**技能名**而不是目录名：例如
    ``vercel-labs/agent-skills@vercel-react-best-practices`` 实际目录是
    ``skills/react-best-practices/SKILL.md``。所以先按目录名精确匹配，再按
    frontmatter ``name`` 匹配（都做宽松比较：忽略 ``:`` ``-`` ``_`` 大小写）。
    """
    if skill:
        candidate = repo_root / skill
        if (candidate / "SKILL.md").is_file():
            return candidate
        target = _normalize_label(skill)
        skill_files = _iter_skill_files(repo_root)
        for skill_file in skill_files:
            if _normalize_label(skill_file.parent.name) == target:
                return skill_file.parent
        for skill_file in skill_files:
            if _normalize_label(_frontmatter_name(skill_file)) == target:
                return skill_file.parent
        return None
    if (repo_root / "SKILL.md").is_file():
        return repo_root
    found = _iter_skill_files(repo_root)
    if len(found) == 1:
        return found[0].parent
    if found:
        # 多技能仓库且没指定：挑 ``skills/`` 之类的浅层目录里的第一个？不做猜测，
        # 交给调用方报错并列出候选（避免装错技能）。
        return None
    return None


def _split_frontmatter(text: str) -> tuple[dict, str]:
    """拆出 SKILL.md 的 YAML frontmatter；没有 frontmatter 时抛 ValueError。"""
    import yaml

    if not text.startswith("---"):
        raise ValueError("生态技能的 SKILL.md 缺少 frontmatter。")
    lines = text.splitlines()
    end = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            end = index
            break
    if end is None or end == 1:
        raise ValueError("生态技能的 SKILL.md frontmatter 不完整。")
    try:
        loaded = yaml.safe_load("\n".join(lines[1:end]))
    except yaml.YAMLError as error:
        raise ValueError(f"生态技能 frontmatter 不是合法 YAML：{error}。") from error
    if not isinstance(loaded, dict):
        raise ValueError("生态技能 frontmatter 不是键值结构。")
    body = "\n".join(lines[end + 1 :]).strip()
    return loaded, body


#: v2 认得的 frontmatter 键（其余键一律折进 metadata，避免被 additionalProperties 拒绝）。
_V2_KEYS = (
    "name",
    "description",
    "version",
    "schema-version",
    "model-invocable",
    "user-invocable",
    "required-tools",
    "required-capabilities",
    "optional-tools",
    "conflicts-with",
    "resource-policy",
    "whenToUse",
    "metadata",
)
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
#: v2 对 description / whenToUse 的长度上限。
_MAX_DESCRIPTION_CHARACTERS = 1024
_MAX_WHEN_TO_USE_CHARACTERS = 500


def normalize_ecosystem_package(package_dir: Path) -> tuple[bool, tuple[str, ...]]:
    """把生态的 v1 SKILL.md 规整成我们要求的 v2 包。

    生态技能（skills.sh / Anthropic 格式）通常只有 ``name`` + ``description``，
    既没有 ``schema-version`` 也没有 ``version``；而我们的包注册表要求 v2。
    安装时规整而不是拒绝，否则 S8 只能装下极少数"恰好写成 v2"的仓库。

    规整规则（无损失，全部可追溯到上游）：

    - 缺 ``schema-version`` → 补 2；缺 ``version`` → 补 ``0.0.0``（与 v1 适配器一致）。
    - v1 的 ``disable-model-invocation: true`` → v2 的 ``model-invocable: false``。
    - 其它未知键（``allowed-tools`` / ``license`` / 嵌套块等）折进 ``metadata``，
      值非标量时用 JSON 文本保留。
    - 上游原文的 sha256 记进 ``metadata.upstream-digest``，正文一字不改。

    返回 ``(是否改写, 被折进 metadata 的键)``；已经是 v2 时返回 ``(False, ())``。
    """
    import hashlib
    import json

    import yaml

    skill_file = package_dir / "SKILL.md"
    if not skill_file.is_file():
        raise ValueError("技能包缺少 SKILL.md。")
    try:
        original = skill_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ValueError("无法读取技能 SKILL.md。") from error

    raw, body = _split_frontmatter(original)
    if isinstance(raw.get("schema-version"), int) and raw["schema-version"] == 2:
        return False, ()

    name = str(raw.get("name") or package_dir.name).strip()
    description = str(raw.get("description") or "").strip()
    if not name:
        raise ValueError("生态技能缺少 name。")
    if len(name) > 64 or "/" in name or "\\" in name:
        raise ValueError(f"生态技能名不合法：{name!r}。")
    if not description:
        raise ValueError("生态技能缺少 description。")
    if len(description) > _MAX_DESCRIPTION_CHARACTERS:
        # v2 约束 description ≤ 1024；生态里确实有超长描述，截断而不是拒绝。
        description = description[:_MAX_DESCRIPTION_CHARACTERS].rstrip()

    version = str(raw.get("version") or "").strip()
    if not _VERSION_RE.match(version):
        version = "0.0.0"

    normalized: dict = {
        "name": name,
        "description": description,
        "version": version,
        "schema-version": 2,
    }
    folded: list[str] = []
    moved_metadata: dict = {}
    for key, value in raw.items():
        if key in ("name", "description", "version", "schema-version"):
            continue
        if key == "disable-model-invocation":
            if value is True or str(value).strip().lower() in ("true", "yes", "1"):
                normalized["model-invocable"] = False
            continue
        if key in _V2_KEYS:
            if key == "whenToUse":
                value = str(value)[:_MAX_WHEN_TO_USE_CHARACTERS]
            normalized[key] = value
            continue
        folded.append(key)
        if isinstance(value, (str, int, float, bool)):
            moved_metadata[key] = value
        else:
            moved_metadata[key] = json.dumps(value, ensure_ascii=False)
    existing_metadata = normalized.get("metadata")
    if isinstance(existing_metadata, dict):
        moved_metadata.update(
            {
                key: value
                for key, value in existing_metadata.items()
                if isinstance(value, (str, int, float, bool))
            }
        )
    moved_metadata["upstream-digest"] = hashlib.sha256(
        original.encode("utf-8")
    ).hexdigest()
    normalized["metadata"] = moved_metadata

    frontmatter = yaml.safe_dump(
        normalized, allow_unicode=True, sort_keys=False, default_flow_style=False
    ).strip()
    skill_file.write_text(
        f"---\n{frontmatter}\n---\n\n{body}\n", encoding="utf-8"
    )
    return True, tuple(folded)
