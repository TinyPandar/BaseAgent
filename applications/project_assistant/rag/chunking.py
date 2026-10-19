"""Document and code chunking with metadata preservation."""

import ast
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


@dataclass
class Chunk:
    """A document or code chunk with source location metadata."""

    content: str
    file_path: str
    line_start: int
    line_end: int
    chunk_type: str  # 'markdown_section', 'python_function', 'python_class', 'python_module'
    parent_section: Optional[str] = None
    function_name: Optional[str] = None
    class_name: Optional[str] = None
    content_hash: str = ""

    def __post_init__(self):
        if not self.content_hash:
            self.content_hash = hashlib.sha256(self.content.encode()).hexdigest()[:16]

    @property
    def doc_id(self) -> str:
        """Unique identifier for this chunk."""
        return f"{self.file_path}:{self.line_start}-{self.line_end}:{self.content_hash}"

    @property
    def citation(self) -> str:
        """Human-readable citation format."""
        return f"{self.file_path}:{self.line_start}-{self.line_end}"


def chunk_document(
    content: str,
    file_path: str,
    max_lines: int = 50,
    overlap_lines: int = 5,
) -> List[Chunk]:
    """
    Chunk a Markdown document by section headers, with fallback to fixed-size chunks.

    Args:
        content: Document text
        file_path: Source file path for metadata
        max_lines: Maximum lines per chunk (for sections exceeding this)
        overlap_lines: Overlapping lines between consecutive chunks

    Returns:
        List of Chunk objects with preserved section hierarchy
    """
    lines = content.splitlines(keepends=False)
    chunks = []

    # Find all headers (markdown # syntax)
    header_pattern = re.compile(r'^(#{1,6})\s+(.+)$')
    headers = []
    for i, line in enumerate(lines):
        match = header_pattern.match(line)
        if match:
            level = len(match.group(1))
            title = match.group(2).strip()
            headers.append((i, level, title))

    if not headers:
        # No headers found, use fixed-size chunking
        return _chunk_text_fixed_size(content, file_path, max_lines, overlap_lines)

    # Group lines by section
    for idx, (line_num, level, title) in enumerate(headers):
        start = line_num
        # Find next header of same or higher level
        end = len(lines)
        for j in range(idx + 1, len(headers)):
            next_line, next_level, _ = headers[j]
            if next_level <= level:
                end = next_line
                break

        section_lines = lines[start:end]
        section_content = '\n'.join(section_lines)

        # If section is too large, split it
        if len(section_lines) > max_lines:
            sub_chunks = _chunk_text_fixed_size(
                section_content,
                file_path,
                max_lines,
                overlap_lines,
                base_line=start + 1,
            )
            for chunk in sub_chunks:
                chunk.parent_section = title
                chunk.chunk_type = 'markdown_section'
            chunks.extend(sub_chunks)
        else:
            chunks.append(Chunk(
                content=section_content,
                file_path=file_path,
                line_start=start + 1,
                line_end=end,
                chunk_type='markdown_section',
                parent_section=title if level > 1 else None,
            ))

    return chunks


def _chunk_text_fixed_size(
    content: str,
    file_path: str,
    max_lines: int,
    overlap_lines: int,
    base_line: int = 1,
) -> List[Chunk]:
    """Split text into fixed-size chunks with overlap."""
    lines = content.splitlines(keepends=False)
    chunks = []

    i = 0
    while i < len(lines):
        end = min(i + max_lines, len(lines))
        chunk_lines = lines[i:end]
        chunks.append(Chunk(
            content='\n'.join(chunk_lines),
            file_path=file_path,
            line_start=base_line + i,
            line_end=base_line + end - 1,
            chunk_type='text_block',
        ))

        # Move forward with overlap
        i += max_lines - overlap_lines
        if i >= len(lines):
            break

    return chunks


def chunk_python_code(
    content: str,
    file_path: str,
    include_module_level: bool = True,
) -> List[Chunk]:
    """
    Chunk Python source code by function and class definitions.

    Args:
        content: Python source code
        file_path: Source file path for metadata
        include_module_level: If True, include module-level code and docstrings

    Returns:
        List of Chunk objects, one per function/class/module-level block
    """
    lines = content.splitlines(keepends=False)
    chunks = []

    try:
        tree = ast.parse(content)
    except SyntaxError:
        # Fallback to text chunking if parse fails
        return _chunk_text_fixed_size(content, file_path, max_lines=100, overlap_lines=10)

    # Extract module-level docstring
    module_docstring = ast.get_docstring(tree)
    if include_module_level and module_docstring:
        # Find docstring location (usually at the top)
        for i, line in enumerate(lines[:10]):
            if '"""' in line or "'''" in line:
                # Simple heuristic: docstring ends at first closing quotes
                end_line = i + 1
                for j in range(i + 1, min(i + 50, len(lines))):
                    if '"""' in lines[j] or "'''" in lines[j]:
                        end_line = j + 1
                        break

                chunks.append(Chunk(
                    content='\n'.join(lines[i:end_line]),
                    file_path=file_path,
                    line_start=i + 1,
                    line_end=end_line,
                    chunk_type='python_module',
                ))
                break

    # Extract functions and classes
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            start_line = node.lineno
            # Find actual end line (ast gives first line only)
            end_line = node.end_lineno if hasattr(node, 'end_lineno') else start_line + 10

            # Extract source lines
            chunk_lines = lines[start_line - 1:end_line]
            chunk_content = '\n'.join(chunk_lines)

            chunk_type = 'python_class' if isinstance(node, ast.ClassDef) else 'python_function'

            chunks.append(Chunk(
                content=chunk_content,
                file_path=file_path,
                line_start=start_line,
                line_end=end_line,
                chunk_type=chunk_type,
                function_name=node.name if chunk_type == 'python_function' else None,
                class_name=node.name if chunk_type == 'python_class' else None,
            ))

    return chunks
