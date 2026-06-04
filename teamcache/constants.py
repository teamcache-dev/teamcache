CONFIG_DIR = ".teamcache"
OBJECTS_DIR = ".teamcache/objects"
SUMMARIES_DIR = ".teamcache/objects/summaries"
SYMBOLS_DIR = ".teamcache/objects/symbols"
LOCAL_DIR = ".teamcache/local"
INDEX_DB = ".teamcache/local/index.sqlite"
EMBEDDINGS_DB = ".teamcache/local/embeddings.sqlite"
CONFIG_FILE = ".teamcache/config.yaml"
SCHEMA_VERSION = "v2"
SYMBOL_SCHEMA_VERSION = "vs1"
MAX_FILE_BYTES = 500 * 1024

# Deprecated: iter_repo_files now uses git ls-files instead of os.walk.
# SKIP_DIRS is kept for backwards compatibility only and is no longer consulted
# by the default file iteration path.
SKIP_DIRS = {
    ".git",
    ".teamcache",
    "node_modules",
    "dist",
    "build",
    "__pycache__",
    ".venv",
    "venv",
    ".next",
    "coverage",
}

SKIP_SUFFIXES = {".lock", ".pyc", ".pyo", ".class", ".map"}

BINARY_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".bmp",
    ".webp",
    ".pdf",
    ".zip",
    ".tar",
    ".gz",
    ".bz2",
    ".xz",
    ".7z",
    ".exe",
    ".dll",
    ".so",
    ".dylib",
    ".wasm",
    ".mp3",
    ".mp4",
    ".wav",
    ".avi",
    ".mov",
    ".bin",
    ".dat",
    ".db",
    ".sqlite",
    ".ttf",
    ".woff",
    ".woff2",
    ".eot",
}

LANGUAGE_BY_SUFFIX = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".rb": "ruby",
    ".php": "php",
    ".swift": "swift",
    ".kt": "kotlin",
    ".sh": "shell",
    ".ps1": "powershell",
    ".md": "markdown",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".xml": "xml",
    ".html": "html",
    ".css": "css",
    ".scss": "scss",
    ".sql": "sql",
}

ENTRY_POINTS = {
    "main.py",
    "app.py",
    "server.py",
    "index.py",
    "run.py",
    "index.js",
    "index.ts",
    "app.js",
    "app.ts",
    "server.js",
    "server.ts",
    "main.go",
    "cmd/main.go",
    "src/main.rs",
    "main.rs",
    "Main.java",
    "Makefile",
    "Dockerfile",
}
