"""Simple reader that reads files of different formats from a directory."""
import logging
from pathlib import Path
from typing import Callable, Dict, List, Optional, Union

from application.parser.file.base import BaseReader
from application.parser.file.base_parser import BaseParser
from application.parser.file.docs_parser import DocxParser, PDFParser
from application.parser.file.epub_parser import EpubParser
from application.parser.file.html_parser import HTMLParser
from application.parser.file.markdown_parser import MarkdownParser
from application.parser.file.rst_parser import RstParser
from application.parser.file.tabular_parser import PandasCSVParser, ExcelParser
from application.parser.file.json_parser import JSONParser
from application.parser.file.pptx_parser import PPTXParser
from application.parser.file.image_parser import ImageParser
from application.parser.schema.base import Document
from application.utils import num_tokens_from_string


# Aztec-fork ingest guard. Skip directories whose entire contents are blob /
# fixture / generated artefact data — including them pumps embedding cost and
# buries useful content in the vector store. A full e2e test zip ingested
# without this filter embedded ~12k chunks from `end-to-end/fixtures/`
# blockchain-state blobs (~$1.50 of OpenAI text-embedding-3-large calls) which
# were never going to be retrieved as answers.
#
# Match happens against any path segment under the input root, so the guard
# catches fixtures/dumps wherever they sit in the tree.
_IGNORED_PATH_SEGMENTS = frozenset(
    {
        "fixtures",
        "dumps",
        "node_modules",
        "target",
        "dist",
        "build",
        "_out",
        "__pycache__",
        ".git",
    }
)


def get_default_file_extractor() -> Dict[str, BaseParser]:
    """Get the default file extractor map (extension -> parser).

    Docling (and its torch/CUDA dependency train) was removed from this
    fork — the corpus is markdown + source code, so the stock parsers
    cover everything that gets ingested.
    """
    return {
        ".pdf": PDFParser(),
        ".docx": DocxParser(),
        ".csv": PandasCSVParser(),
        ".xlsx": ExcelParser(),
        ".epub": EpubParser(),
        ".md": MarkdownParser(),
        ".rst": RstParser(),
        ".html": HTMLParser(),
        ".mdx": MarkdownParser(),
        ".json": JSONParser(),
        ".pptx": PPTXParser(),
        ".png": ImageParser(),
        ".jpg": ImageParser(),
        ".jpeg": ImageParser(),
    }


# For backwards compatibility
DEFAULT_FILE_EXTRACTOR: Dict[str, BaseParser] = get_default_file_extractor()


class SimpleDirectoryReader(BaseReader):
    """Simple directory reader.

    Can read files into separate documents, or concatenates
    files into one document text.

    Args:
        input_dir (str): Path to the directory.
        input_files (List): List of file paths to read (Optional; overrides input_dir)
        exclude_hidden (bool): Whether to exclude hidden files (dotfiles).
        errors (str): how encoding and decoding errors are to be handled,
              see https://docs.python.org/3/library/functions.html#open
        recursive (bool): Whether to recursively search in subdirectories.
            False by default.
        required_exts (Optional[List[str]]): List of required extensions.
            Default is None.
        file_extractor (Optional[Dict[str, BaseParser]]): A mapping of file
            extension to a BaseParser class that specifies how to convert that file
            to text. See DEFAULT_FILE_EXTRACTOR.
        num_files_limit (Optional[int]): Maximum number of files to read.
            Default is None.
        file_metadata (Optional[Callable[str, Dict]]): A function that takes
            in a filename and returns a Dict of metadata for the Document.
            Default is None.
    """

    def __init__(
            self,
            input_dir: Optional[str] = None,
            input_files: Optional[List] = None,
            exclude_hidden: bool = True,
            errors: str = "ignore",
            recursive: bool = True,
            required_exts: Optional[List[str]] = None,
            file_extractor: Optional[Dict[str, BaseParser]] = None,
            num_files_limit: Optional[int] = None,
            file_metadata: Optional[Callable[[str], Dict]] = None,
    ) -> None:
        """Initialize with parameters."""
        super().__init__()

        if not input_dir and not input_files:
            raise ValueError("Must provide either `input_dir` or `input_files`.")

        self.errors = errors

        self.recursive = recursive
        self.exclude_hidden = exclude_hidden
        # Normalize extensions to lowercase for case-insensitive matching
        self.required_exts = (
            [ext.lower() for ext in required_exts] if required_exts else None
        )
        self.num_files_limit = num_files_limit

        if input_files:
            self.input_files = []
            for path in input_files:
                logging.debug(f"SimpleDirectoryReader input file: {path}")
                input_file = Path(path)
                self.input_files.append(input_file)
        elif input_dir:
            self.input_dir = Path(input_dir)
            self.input_files = self._add_files(self.input_dir)

        self.file_extractor = file_extractor or DEFAULT_FILE_EXTRACTOR
        self.file_metadata = file_metadata

    def _is_ignored_path(self, path: Path) -> bool:
        """True if any segment of ``path`` (relative to input_dir) is on the
        Aztec-fork ignore list — keeps fixture/dump/artefact dirs out of
        the vector store."""
        base = getattr(self, "input_dir", None)
        try:
            rel_parts = path.relative_to(base).parts if base else path.parts
        except ValueError:
            rel_parts = path.parts
        return any(seg in _IGNORED_PATH_SEGMENTS for seg in rel_parts)

    def _add_files(self, input_dir: Path) -> List[Path]:
        """Add files."""
        input_files = sorted(input_dir.iterdir())
        new_input_files = []
        dirs_to_explore = []
        for input_file in input_files:
            if self._is_ignored_path(input_file):
                continue
            if input_file.is_dir():
                if self.recursive:
                    dirs_to_explore.append(input_file)
            elif self.exclude_hidden and input_file.name.startswith("."):
                continue
            elif (
                    self.required_exts is not None
                    and input_file.suffix.lower() not in self.required_exts
            ):
                continue
            else:
                new_input_files.append(input_file)

        for dir_to_explore in dirs_to_explore:
            sub_input_files = self._add_files(dir_to_explore)
            new_input_files.extend(sub_input_files)

        if self.num_files_limit is not None and self.num_files_limit > 0:
            new_input_files = new_input_files[0: self.num_files_limit]

        # print total number of files added
        logging.debug(
            f"> [SimpleDirectoryReader] Total files added: {len(new_input_files)}"
        )

        return new_input_files

    def load_data(self, concatenate: bool = False) -> List[Document]:
        """Load data from the input directory.

        Args:
            concatenate (bool): whether to concatenate all files into one document.
                If set to True, file metadata is ignored.
                False by default.

        Returns:
            List[Document]: A list of documents.
        """
        data: Union[str, List[str]] = ""
        data_list: List[str] = []
        metadata_list = []
        self.file_token_counts = {}
        
        for input_file in self.input_files:
            suffix_lower = input_file.suffix.lower()
            parser_metadata = {}
            if suffix_lower in self.file_extractor:
                parser = self.file_extractor[suffix_lower]
                if not parser.parser_config_set:
                    parser.init_parser()
                data = parser.parse_file(input_file, errors=self.errors)
                parser_metadata = parser.get_file_metadata(input_file)
            else:
                # do standard read
                with open(input_file, "r", errors=self.errors) as f:
                    data = f.read()
            
            # Calculate token count for this file
            if isinstance(data, List):
                file_tokens = sum(num_tokens_from_string(str(d)) for d in data)
            else:
                file_tokens = num_tokens_from_string(str(data))
            
            full_path = str(input_file.resolve())
            self.file_token_counts[full_path] = file_tokens
            
            base_metadata = {
                'title': input_file.name,
                'token_count': file_tokens,
            }
            if parser_metadata:
                base_metadata.update(parser_metadata)
            
            if hasattr(self, 'input_dir'):
                try:
                    relative_path = str(input_file.relative_to(self.input_dir))
                    base_metadata['source'] = relative_path
                except ValueError:
                    base_metadata['source'] = str(input_file)
            else:
                base_metadata['source'] = str(input_file)

            # Aztec-fork: tag apiref-shaped files so downstream chunking
            # exempts them from the <50 token discard. The apiref
            # transform (scripts/ingest/noir_apiref.py) emits files
            # named ``foo.nr.md`` — a code-extension followed by ``.md``
            # is the convention. See PLAN-rag-apiref.md / Phase 1.4.
            name = input_file.name.lower()
            if name.endswith(('.nr.md', '.ts.md', '.sol.md')):
                base_metadata['chunk_type'] = 'apiref'

            if self.file_metadata is not None:
                custom_metadata = self.file_metadata(input_file.name)
                base_metadata.update(custom_metadata)

            if isinstance(data, List):
                # Extend data_list with each item in the data list
                data_list.extend([str(d) for d in data])
                metadata_list.extend([base_metadata for _ in data])
            else:
                data_list.append(str(data))
                metadata_list.append(base_metadata)
        
        # Build directory structure if input_dir is provided
        if hasattr(self, 'input_dir'):
            self.directory_structure = self.build_directory_structure(self.input_dir)
            logging.info("Directory structure built successfully")
        else:
            self.directory_structure = {}

        if concatenate:
            return [Document("\n".join(data_list))]
        elif self.file_metadata is not None:
            return [Document(d, extra_info=m) for d, m in zip(data_list, metadata_list)]
        else:
            return [Document(d) for d in data_list]

    def build_directory_structure(self, base_path):
        """Build a dictionary representing the directory structure.

        Args:
            base_path: The base path to start building the structure from.

        Returns:
            dict: A nested dictionary representing the directory structure.
        """
        import mimetypes
        
        def build_tree(path):
            """Helper function to recursively build the directory tree."""
            result = {}
            
            for item in path.iterdir():
                if self.exclude_hidden and item.name.startswith('.'):
                    continue
                if self._is_ignored_path(item):
                    continue

                if item.is_dir():
                    subtree = build_tree(item)
                    if subtree:
                        result[item.name] = subtree
                else:
                    if self.required_exts is not None and item.suffix.lower() not in self.required_exts:
                        continue
                    
                    full_path = str(item.resolve())
                    file_size_bytes = item.stat().st_size
                    mime_type = mimetypes.guess_type(item.name)[0] or "application/octet-stream"
                    
                    file_info = {
                        "type": mime_type,
                        "size_bytes": file_size_bytes
                    }
                    
                    if hasattr(self, 'file_token_counts') and full_path in self.file_token_counts:
                        file_info["token_count"] = self.file_token_counts[full_path]
                        
                    result[item.name] = file_info
                    
            return result
        
        return build_tree(Path(base_path))
