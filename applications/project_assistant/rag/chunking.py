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
    max_lines: int = 200,
    overlap_lines: int = 5,
) -> List[Chunk]:
    """Chunk top-level definitions without nested duplicates.

    Definitions exceeding max_lines use overlapping source-line windows, keeping
    their function/class metadata. Classes fitting max_lines stay whole; larger
    classes split into a preamble and methods. Module statements are included
    when include_module_level.
    """
    if max_lines <= 0 or not 0 <= overlap_lines < max_lines:
        raise ValueError("require max_lines > 0 and 0 <= overlap_lines < max_lines")
    lines = content.splitlines(keepends=False)
    chunks = []

    try:
        tree = ast.parse(content)
    except SyntaxError:
        return _chunk_text_fixed_size(content, file_path, max_lines, overlap_lines)

    def append_block(start, end, chunk_type, function_name=None, class_name=None):
        blocks = _chunk_text_fixed_size(
            '\n'.join(lines[start - 1:end]), file_path, max_lines,
            overlap_lines, base_line=start,
        )
        for chunk in blocks:
            chunk.chunk_type = chunk_type
            chunk.function_name = function_name
            chunk.class_name = class_name
            chunks.append(chunk)
            if chunk.line_end == end:
                break

    def definition_start(node):
        return min([node.lineno] + [d.lineno for d in node.decorator_list])

    if include_module_level:
        module_nodes = []
        for node in [*tree.body, None]:
            if node is None or isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if module_nodes:
                    append_block(module_nodes[0].lineno, module_nodes[-1].end_lineno,
                                 'python_module')
                    module_nodes = []
            else:
                module_nodes.append(node)

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            append_block(definition_start(node), node.end_lineno,
                         'python_function', function_name=node.name)
        elif isinstance(node, ast.ClassDef):
            start = definition_start(node)
            if node.end_lineno - start + 1 <= max_lines:
                append_block(start, node.end_lineno, 'python_class', class_name=node.name)
                continue
            methods = [child for child in node.body
                       if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))]
            header_end = definition_start(methods[0]) - 1 if methods else node.end_lineno
            append_block(start, header_end,
                         'python_class', class_name=node.name)
            for method in methods:
                append_block(definition_start(method), method.end_lineno,
                             'python_function', function_name=method.name, class_name=node.name)

    return chunks
