"""Anthropic memory_20250818 client-side tool implementation.

This tool implements the six standard filesystem operations of the Anthropic memory tool:
- view: View a memory file or directory.
- create: Create a new memory file.
- str_replace: Perform precise string replacement in a memory file.
- insert: Insert text at a specific line in a memory file.
- delete: Remove a memory file.
- rename: Move/rename a memory file.

All virtual files are saved as MemoryRecord objects in the metacog store, using a
stable, deterministic ID derived from the virtual path to enable rapid, direct retrieval.
"""

from __future__ import annotations

import hashlib
from typing import Any

# Safe fallback for BetaAbstractMemoryTool if anthropic is not installed.
try:
    from anthropic.lib.tools import BetaAbstractMemoryTool
except ImportError:

    class BetaAbstractMemoryTool:  # type: ignore
        """Mock fallback for BetaAbstractMemoryTool when anthropic SDK is absent."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass


from omniagentos.metacog.contracts import MemoryRecord
from omniagentos.metacog.service import MetacogService


def _get_attr(obj: Any, name: str, default: Any = None) -> Any:
    """Safely get an attribute from a dictionary, model, or custom object."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


class MetacogMemoryTool(BetaAbstractMemoryTool):
    """Subclass of BetaAbstractMemoryTool backed by the metacog sqlite store.

    This maps the Virtual Filesystem API of the Anthropic memory_20250818 tool
    onto individual MemoryRecord objects within the SQLite-backed MetacogStore.
    """

    def __init__(self, service: MetacogService | None = None) -> None:
        """Initialize the tool with an optional MetacogService."""
        super().__init__()
        self._service = service

    @property
    def service(self) -> MetacogService:
        """Lazily initialize and return the MetacogService."""
        if self._service is None:
            from omniagentos.contracts import default_db_path

            self._service = MetacogService(db_path=default_db_path())
        return self._service

    @property
    def store(self) -> Any:
        """Access the underlying MetacogStore."""
        return self.service.store

    def _normalize_and_validate_path(self, path: str) -> str:
        """Resolve and validate a virtual path, preventing directory traversal.

        Ensures that all memory tool file paths are safe and reside inside `/memories`.
        """
        if not path:
            raise ValueError("Path cannot be empty.")

        # Uniform separators and strip whitespace
        p = path.replace("\\", "/").strip()

        # Split and prevent any traversal using ".." or relative "."
        parts = []
        for part in p.split("/"):
            if part == "..":
                raise ValueError(f"Directory traversal sequences are forbidden: {path}")
            if part == "." or not part:
                continue
            parts.append(part)

        # Enforce that the top-level virtual directory is 'memories'
        if not parts or parts[0] != "memories":
            parts.insert(0, "memories")

        normalized = "/" + "/".join(parts)
        return normalized

    def _path_to_id(self, path: str) -> str:
        """Map a virtual path to a deterministic MemoryRecord ID."""
        normalized = path.lower().strip()
        h = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
        return f"mem_vfile_{h}"

    def _is_directory(self, path: str) -> bool:
        """Determine if a given virtual path acts as a directory."""
        # /memories is always a directory.
        if path == "/memories":
            return True
        return False

    def view(self, command: Any) -> str:
        """Retrieve the contents of a virtual memory file or directory."""
        raw_path = _get_attr(command, "path")
        path = self._normalize_and_validate_path(raw_path)

        if self._is_directory(path):
            # List directory: return a tree or list of all virtual files
            mems = self.store.list_memories()
            files = []
            for mem in mems:
                vpath = mem.applicability.get("virtual_path")
                if vpath and mem.applicability.get("is_virtual_file"):
                    files.append(vpath)

            if not files:
                return "[Directory Contents of /memories]\n(Empty directory)"

            sorted_files = sorted(list(set(files)))
            return "[Directory Contents of /memories]\n" + "\n".join(f"- {f}" for f in sorted_files)

        # File view
        mem_id = self._path_to_id(path)
        record = self.store.get_memory(mem_id)
        if not record:
            raise FileNotFoundError(f"File '{path}' does not exist.")

        # Format with 1-based line numbers as required by Claude's tool interface
        lines = record.statement.splitlines()
        formatted = [f"{i + 1}: {line}" for i, line in enumerate(lines)]
        return "\n".join(formatted)

    def create(self, command: Any) -> str:
        """Create a new virtual memory file or overwrite an existing one."""
        raw_path = _get_attr(command, "path")
        path = self._normalize_and_validate_path(raw_path)
        file_text = _get_attr(command, "file_text") or _get_attr(command, "content") or ""

        # Map type dynamically based on filename to align with MemoryRecord standards
        filename = path.split("/")[-1].lower()
        if "procedure" in filename:
            m_type = "procedure"
        elif "lesson" in filename:
            m_type = "lesson"
        elif "strategy" in filename:
            m_type = "strategy"
        elif "warning" in filename:
            m_type = "warning"
        elif "benchmark" in filename:
            m_type = "benchmark"
        else:
            m_type = "fact"

        mem_id = self._path_to_id(path)
        record = MemoryRecord(
            id=mem_id,
            type=m_type,  # type: ignore[arg-type]
            statement=file_text,
            promotion_status="promoted",
            applicability={"virtual_path": path, "is_virtual_file": True},
        )
        self.store.upsert_memory(record)
        return f"File created successfully at {path}."

    def str_replace(self, command: Any) -> str:
        """Perform exact string replacement in a virtual memory file."""
        raw_path = _get_attr(command, "path")
        path = self._normalize_and_validate_path(raw_path)
        old_str = _get_attr(command, "old_str")
        new_str = _get_attr(command, "new_str")

        if old_str is None or new_str is None:
            raise ValueError("Parameters 'old_str' and 'new_str' must be provided.")

        mem_id = self._path_to_id(path)
        record = self.store.get_memory(mem_id)
        if not record:
            raise FileNotFoundError(f"File '{path}' does not exist.")

        content = record.statement
        count = content.count(old_str)
        if count == 0:
            raise ValueError(f"String '{old_str}' was not found in '{path}'.")
        if count > 1:
            raise ValueError(
                f"String '{old_str}' is ambiguous in '{path}' ({count} matches found)."
            )

        new_content = content.replace(old_str, new_str)
        record.statement = new_content
        self.store.upsert_memory(record)
        return f"Successfully replaced text in '{path}'."

    def insert(self, command: Any) -> str:
        """Insert text at a specific line number (1-based index) in a virtual memory file."""
        raw_path = _get_attr(command, "path")
        path = self._normalize_and_validate_path(raw_path)
        line_number = _get_attr(command, "line_number")
        text = _get_attr(command, "text")

        if line_number is None or text is None:
            raise ValueError("Parameters 'line_number' and 'text' must be provided.")

        try:
            line_no = int(line_number)
        except (ValueError, TypeError):
            raise ValueError(f"Invalid line number: {line_number}") from None

        mem_id = self._path_to_id(path)
        record = self.store.get_memory(mem_id)
        if not record:
            raise FileNotFoundError(f"File '{path}' does not exist.")

        lines = record.statement.splitlines()
        idx = line_no - 1
        if idx < 0:
            idx = 0

        if idx >= len(lines):
            lines.append(text)
        else:
            lines.insert(idx, text)

        record.statement = "\n".join(lines)
        self.store.upsert_memory(record)
        return f"Successfully inserted text at line {line_no} in '{path}'."

    def delete(self, command: Any) -> str:
        """Delete a virtual memory file from the store."""
        raw_path = _get_attr(command, "path")
        path = self._normalize_and_validate_path(raw_path)

        mem_id = self._path_to_id(path)
        record = self.store.get_memory(mem_id)
        if not record:
            raise FileNotFoundError(f"File '{path}' does not exist.")

        self.store.delete_memory(mem_id)
        return f"Successfully deleted '{path}'."

    def rename(self, command: Any) -> str:
        """Rename/move a virtual memory file, retaining its content."""
        raw_path = _get_attr(command, "path")
        raw_new_path = _get_attr(command, "new_path")

        path = self._normalize_and_validate_path(raw_path)
        new_path = self._normalize_and_validate_path(raw_new_path)

        old_id = self._path_to_id(path)
        new_id_str = self._path_to_id(new_path)

        record = self.store.get_memory(old_id)
        if not record:
            raise FileNotFoundError(f"File '{path}' does not exist.")

        # Create new record at new location
        new_record = MemoryRecord(
            id=new_id_str,
            type=record.type,
            statement=record.statement,
            promotion_status=record.promotion_status,
            applicability={"virtual_path": new_path, "is_virtual_file": True},
        )
        self.store.upsert_memory(new_record)
        self.store.delete_memory(old_id)
        return f"Successfully renamed '{path}' to '{new_path}'."
