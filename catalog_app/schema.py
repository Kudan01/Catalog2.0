from __future__ import annotations


# ASCII "CAT2" stored as a 32-bit SQLite application identifier.
APPLICATION_ID = 0x43415432
SCHEMA_VERSION = 5

EXPECTED_TABLES = frozenset({
    "scan_sessions",
    "folders",
    "media_files",
    "scan_folders",
    "scan_media_files",
    "scan_errors",
    "thumbnails",
    "video_previews",
    "folder_preview_items",
})

EXPECTED_INDEXES = frozenset({
    "idx_scan_sessions_status_started",
    "idx_folders_parent_listing",
    "idx_folders_last_scan",
    "idx_media_folder_listing",
    "idx_media_type_listing",
    "idx_media_last_scan",
    "idx_scan_folders_parent",
    "idx_scan_media_folder_listing",
    "idx_scan_errors_scan",
    "idx_thumbnails_cleanup",
    "idx_thumbnails_media_kind",
    "idx_thumbnails_status",
    "idx_video_previews_media",
    "idx_folder_preview_media",
})

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE scan_sessions (
        id INTEGER PRIMARY KEY,
        scan_type TEXT NOT NULL
            CHECK (scan_type IN ('full', 'branch')),
        scope_rel_path TEXT NOT NULL DEFAULT '',
        scope_path_key TEXT NOT NULL DEFAULT '',
        started_at REAL NOT NULL,
        finished_at REAL,
        status TEXT NOT NULL
            CHECK (status IN ('running', 'completed', 'failed', 'interrupted', 'cancelled')),
        folder_count INTEGER NOT NULL DEFAULT 0
            CHECK (folder_count >= 0),
        media_count INTEGER NOT NULL DEFAULT 0
            CHECK (media_count >= 0),
        error_count INTEGER NOT NULL DEFAULT 0
            CHECK (error_count >= 0),
        active_scan_id_at_stage INTEGER
            REFERENCES scan_sessions(id),
        CHECK (
            (status = 'running' AND finished_at IS NULL)
            OR
            (status IN ('completed', 'failed', 'interrupted', 'cancelled') AND finished_at IS NOT NULL)
        )
    )
    """,
    """
    CREATE TABLE folders (
        id INTEGER PRIMARY KEY,
        rel_path TEXT NOT NULL,
        path_key TEXT NOT NULL UNIQUE,
        parent_id INTEGER
            REFERENCES folders(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        depth INTEGER NOT NULL
            CHECK (depth >= 0),
        sort_key TEXT NOT NULL,
        last_successful_scan_id INTEGER NOT NULL
            REFERENCES scan_sessions(id),
        is_available INTEGER NOT NULL DEFAULT 1
            CHECK (is_available IN (0, 1)),

        direct_child_count INTEGER NOT NULL DEFAULT 0
            CHECK (direct_child_count >= 0),
        direct_image_count INTEGER NOT NULL DEFAULT 0
            CHECK (direct_image_count >= 0),
        direct_gif_count INTEGER NOT NULL DEFAULT 0
            CHECK (direct_gif_count >= 0),
        direct_video_count INTEGER NOT NULL DEFAULT 0
            CHECK (direct_video_count >= 0),
        direct_other_count INTEGER NOT NULL DEFAULT 0
            CHECK (direct_other_count >= 0),

        recursive_folder_count INTEGER NOT NULL DEFAULT 0
            CHECK (recursive_folder_count >= 0),
        recursive_image_count INTEGER NOT NULL DEFAULT 0
            CHECK (recursive_image_count >= 0),
        recursive_gif_count INTEGER NOT NULL DEFAULT 0
            CHECK (recursive_gif_count >= 0),
        recursive_video_count INTEGER NOT NULL DEFAULT 0
            CHECK (recursive_video_count >= 0),
        recursive_other_count INTEGER NOT NULL DEFAULT 0
            CHECK (recursive_other_count >= 0),

        CHECK (
            (depth = 0 AND parent_id IS NULL AND rel_path = '')
            OR
            (depth > 0 AND parent_id IS NOT NULL AND rel_path <> '')
        )
    )
    """,
    """
    CREATE TABLE media_files (
        id INTEGER PRIMARY KEY,
        rel_path TEXT NOT NULL,
        path_key TEXT NOT NULL UNIQUE,
        folder_id INTEGER NOT NULL
            REFERENCES folders(id) ON DELETE CASCADE,
        file_name TEXT NOT NULL,
        extension TEXT NOT NULL,
        media_type TEXT NOT NULL
            CHECK (media_type IN ('image', 'gif', 'video', 'other')),
        size_bytes INTEGER NOT NULL
            CHECK (size_bytes >= 0),
        modified_time REAL NOT NULL,
        sort_key TEXT NOT NULL,
        last_successful_scan_id INTEGER NOT NULL
            REFERENCES scan_sessions(id),
        is_available INTEGER NOT NULL DEFAULT 1
            CHECK (is_available IN (0, 1))
    )
    """,
    """
    CREATE TABLE scan_folders (
        scan_id INTEGER NOT NULL
            REFERENCES scan_sessions(id) ON DELETE CASCADE,
        rel_path TEXT NOT NULL,
        path_key TEXT NOT NULL,
        parent_path_key TEXT,
        name TEXT NOT NULL,
        depth INTEGER NOT NULL
            CHECK (depth >= 0),
        sort_key TEXT NOT NULL,
        PRIMARY KEY (scan_id, path_key),
        CHECK (
            (depth = 0 AND parent_path_key IS NULL AND rel_path = '')
            OR
            (depth > 0 AND parent_path_key IS NOT NULL AND rel_path <> '')
        )
    )
    """,
    """
    CREATE TABLE scan_media_files (
        scan_id INTEGER NOT NULL,
        rel_path TEXT NOT NULL,
        path_key TEXT NOT NULL,
        folder_path_key TEXT NOT NULL,
        file_name TEXT NOT NULL,
        extension TEXT NOT NULL,
        media_type TEXT NOT NULL
            CHECK (media_type IN ('image', 'gif', 'video', 'other')),
        size_bytes INTEGER NOT NULL
            CHECK (size_bytes >= 0),
        modified_time REAL NOT NULL,
        sort_key TEXT NOT NULL,
        PRIMARY KEY (scan_id, path_key),
        FOREIGN KEY (scan_id, folder_path_key)
            REFERENCES scan_folders(scan_id, path_key)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE scan_errors (
        id INTEGER PRIMARY KEY,
        scan_id INTEGER NOT NULL
            REFERENCES scan_sessions(id) ON DELETE CASCADE,
        rel_path TEXT,
        operation TEXT NOT NULL,
        error_type TEXT NOT NULL,
        message TEXT NOT NULL,
        created_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE thumbnails (
        id INTEGER PRIMARY KEY,
        media_id INTEGER NOT NULL
            REFERENCES media_files(id) ON DELETE CASCADE,
        thumbnail_type TEXT NOT NULL
            CHECK (thumbnail_type IN (
                'photo_tile',
                'gif_preview',
                'video_poster',
                'video_frame',
                'folder_preview'
            )),
        cache_class TEXT NOT NULL
            CHECK (cache_class IN ('dynamic', 'protected')),
        variant_key TEXT NOT NULL DEFAULT '',
        output_rel_path TEXT NOT NULL UNIQUE,
        width INTEGER NOT NULL
            CHECK (width > 0),
        height INTEGER NOT NULL
            CHECK (height > 0),
        file_size_bytes INTEGER NOT NULL
            CHECK (file_size_bytes >= 0),
        source_size_bytes INTEGER NOT NULL
            CHECK (source_size_bytes >= 0),
        source_modified_time REAL NOT NULL,
        algorithm_version TEXT NOT NULL,
        status TEXT NOT NULL
            CHECK (status IN ('pending', 'ready', 'stale', 'missing', 'error')),
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        last_used_at REAL,
        error_message TEXT,
        UNIQUE (media_id, thumbnail_type, variant_key)
    )
    """,
    """
    CREATE TABLE video_previews (
        id INTEGER PRIMARY KEY,
        media_id INTEGER NOT NULL
            REFERENCES media_files(id) ON DELETE CASCADE,
        preview_kind TEXT NOT NULL
            CHECK (preview_kind IN ('poster', 'screen')),
        index_no INTEGER NOT NULL
            CHECK (index_no BETWEEN 0 AND 4),
        output_rel_path TEXT NOT NULL UNIQUE,
        width INTEGER NOT NULL
            CHECK (width > 0),
        height INTEGER NOT NULL
            CHECK (height > 0),
        size_bytes INTEGER NOT NULL
            CHECK (size_bytes >= 0),
        source_size_bytes INTEGER NOT NULL
            CHECK (source_size_bytes >= 0),
        source_modified_time REAL NOT NULL,
        created_at REAL NOT NULL,
        UNIQUE (media_id, index_no),
        CHECK (
            (preview_kind = 'poster' AND index_no = 0)
            OR
            (preview_kind = 'screen' AND index_no BETWEEN 1 AND 4)
        )
    )
    """,
    """
    CREATE TABLE folder_preview_items (
        folder_id INTEGER NOT NULL
            REFERENCES folders(id) ON DELETE CASCADE,
        selection_type TEXT NOT NULL
            CHECK (selection_type IN ('auto', 'auto_parent', 'manual')),
        position INTEGER NOT NULL
            CHECK (position BETWEEN 1 AND 12),
        media_id INTEGER NOT NULL
            REFERENCES media_files(id) ON DELETE CASCADE,
        PRIMARY KEY (folder_id, selection_type, position),
        UNIQUE (folder_id, selection_type, media_id)
    )
    """,
    """
    CREATE INDEX idx_scan_sessions_status_started
        ON scan_sessions(status, started_at)
    """,
    """
    CREATE INDEX idx_folders_parent_listing
        ON folders(parent_id, is_available, sort_key, id)
    """,
    """
    CREATE INDEX idx_folders_last_scan
        ON folders(last_successful_scan_id)
    """,
    """
    CREATE INDEX idx_media_folder_listing
        ON media_files(folder_id, media_type, is_available, sort_key, id)
    """,
    """
    CREATE INDEX idx_media_type_listing
        ON media_files(media_type, is_available, sort_key, id)
    """,
    """
    CREATE INDEX idx_media_last_scan
        ON media_files(last_successful_scan_id)
    """,
    """
    CREATE INDEX idx_scan_folders_parent
        ON scan_folders(scan_id, parent_path_key, sort_key)
    """,
    """
    CREATE INDEX idx_scan_media_folder_listing
        ON scan_media_files(scan_id, folder_path_key, media_type, sort_key)
    """,
    """
    CREATE INDEX idx_scan_errors_scan
        ON scan_errors(scan_id, id)
    """,
    """
    CREATE INDEX idx_thumbnails_cleanup
        ON thumbnails(cache_class, status, last_used_at, id)
    """,
    """
    CREATE INDEX idx_thumbnails_media_kind
        ON thumbnails(media_id, thumbnail_type, variant_key)
    """,
    """
    CREATE INDEX idx_thumbnails_status
        ON thumbnails(status, cache_class, thumbnail_type)
    """,
    """
    CREATE INDEX idx_video_previews_media
        ON video_previews(media_id, index_no)
    """,
    """
    CREATE INDEX idx_folder_preview_media
        ON folder_preview_items(media_id)
    """,
)
