#!/usr/bin/env php
<?php

declare(strict_types=1);

/**
 * Idempotent NextGEN/Imagely server-folder importer.
 *
 * Safe for cron: never overwrites or deletes existing galleries/images.
 *
 * Usage examples:
 *   php scripts/wp_ngg_idempotent_import.php --wp-load=/home/site/www/wp-load.php --base-dir=/home/site/www/wp-content/zoepham
 *   php scripts/wp_ngg_idempotent_import.php --wp-load=/home/site/www/wp-load.php --folder=/home/site/www/wp-content/zoepham/20260425-splash-splash
 */

if (PHP_SAPI !== 'cli') {
    fwrite(STDERR, "This script must run in CLI.\n");
    exit(1);
}

$options = getopt('', [
    'wp-load:',
    'base-dir::',
    'folder::',
    'processed-json::',
]);

$wpLoad = isset($options['wp-load']) ? (string) $options['wp-load'] : '';
if ($wpLoad === '' || !is_file($wpLoad)) {
    fwrite(STDERR, "Missing/invalid --wp-load path.\n");
    exit(1);
}

require_once $wpLoad;

if (!function_exists('wp_normalize_path')) {
    fwrite(STDERR, "WordPress bootstrap failed.\n");
    exit(1);
}

global $wpdb;

$galleryTable = $wpdb->prefix . 'ngg_gallery';
$pictureTable = $wpdb->prefix . 'ngg_pictures';

$galleryExists = (bool) $wpdb->get_var($wpdb->prepare('SHOW TABLES LIKE %s', $galleryTable));
$pictureExists = (bool) $wpdb->get_var($wpdb->prepare('SHOW TABLES LIKE %s', $pictureTable));

if (!$galleryExists || !$pictureExists) {
    fwrite(STDERR, "NextGEN tables not found: expected {$galleryTable} and {$pictureTable}.\n");
    exit(1);
}

const STATE_OPTION_KEY = 'zoepham_ngg_processed_folders';

$processedJson = isset($options['processed-json'])
    ? (string) $options['processed-json']
    : wp_normalize_path(WP_CONTENT_DIR . '/uploads/zoepham_ngg_processed_folders.json');

$stats = [
    'imported_folders' => 0,
    'skipped_folders' => 0,
    'imported_images' => 0,
    'skipped_duplicate_images' => 0,
];

$processedState = load_processed_state(STATE_OPTION_KEY, $processedJson);

$folders = resolve_target_folders($options);
if (count($folders) === 0) {
    log_line('INFO', 'No folders to process.');
    exit(0);
}

foreach ($folders as $folderAbs) {
    process_folder($folderAbs, $processedState, $galleryTable, $pictureTable, $stats);
}

save_processed_state(STATE_OPTION_KEY, $processedJson, $processedState);

log_line('SUMMARY', sprintf(
    'imported_folders=%d skipped_folders=%d imported_images=%d skipped_duplicate_images=%d',
    $stats['imported_folders'],
    $stats['skipped_folders'],
    $stats['imported_images'],
    $stats['skipped_duplicate_images']
));

exit(0);

function resolve_target_folders(array $options): array
{
    $folders = [];

    if (isset($options['folder'])) {
        $input = $options['folder'];
        if (is_array($input)) {
            foreach ($input as $f) {
                $folders[] = wp_normalize_path((string) $f);
            }
        } else {
            $folders[] = wp_normalize_path((string) $input);
        }
        return array_values(array_unique($folders));
    }

    $baseDir = isset($options['base-dir']) && (string) $options['base-dir'] !== ''
        ? wp_normalize_path((string) $options['base-dir'])
        : wp_normalize_path(WP_CONTENT_DIR . '/zoepham');

    if (!is_dir($baseDir)) {
        log_line('WARN', "Base dir does not exist: {$baseDir}");
        return [];
    }

    $entries = scandir($baseDir);
    if ($entries === false) {
        return [];
    }

    foreach ($entries as $entry) {
        if ($entry === '.' || $entry === '..') {
            continue;
        }
        $full = wp_normalize_path($baseDir . '/' . $entry);
        if (is_dir($full)) {
            $folders[] = $full;
        }
    }

    sort($folders);
    return $folders;
}

function process_folder(string $folderAbs, array &$processedState, string $galleryTable, string $pictureTable, array &$stats): void
{
    global $wpdb;

    $real = realpath($folderAbs);
    if ($real === false || !is_dir($real)) {
        log_line('WARN', "Folder missing, skipped: {$folderAbs}");
        $stats['skipped_folders']++;
        return;
    }

    $folderAbs = wp_normalize_path($real);

    $wpContent = wp_normalize_path(realpath(WP_CONTENT_DIR) ?: WP_CONTENT_DIR);
    if (strpos($folderAbs, $wpContent . '/') !== 0) {
        log_line('WARN', "Folder outside /wp-content/, skipped: {$folderAbs}");
        $stats['skipped_folders']++;
        return;
    }

    $folderRel = '/' . ltrim(substr($folderAbs, strlen($wpContent)), '/');
    $folderName = basename($folderAbs);

    $alreadyProcessed = isset($processedState['folders'][$folderRel]);
    if ($alreadyProcessed) {
        log_line('SKIPPED', 'SKIPPED: folder already imported');
    }

    $gallery = find_existing_gallery($galleryTable, $folderRel, $folderName);
    $createdGallery = false;

    if ($gallery !== null) {
        log_line('INFO', "Gallery exists; skip create: gid={$gallery['gid']} path={$gallery['path']}");
        $galleryId = (int) $gallery['gid'];
    } else {
        $galleryId = create_gallery_record($galleryTable, $folderRel, $folderName);
        if ($galleryId <= 0) {
            log_line('ERROR', "Failed to create gallery for {$folderRel}");
            $stats['skipped_folders']++;
            return;
        }
        $createdGallery = true;
        log_line('IMPORTED_FOLDER', "Created gallery gid={$galleryId} for {$folderRel}");
    }

    $existingNames = load_gallery_filenames($pictureTable, $galleryId);
    $imageFiles = list_image_files($folderAbs);

    $newCount = 0;
    $dupCount = 0;

    foreach ($imageFiles as $imageFile) {
        $filename = basename($imageFile);
        $key = strtolower($filename);

        if (isset($existingNames[$key])) {
            $dupCount++;
            $stats['skipped_duplicate_images']++;
            log_line('SKIPPED_IMAGE', "Duplicate in gallery {$galleryId}: {$filename}");
            continue;
        }

        $ok = insert_picture_record($pictureTable, $galleryId, $filename);
        if ($ok) {
            $newCount++;
            $stats['imported_images']++;
            $existingNames[$key] = true;
            log_line('IMPORTED_IMAGE', "gid={$galleryId} {$filename}");
        } else {
            log_line('ERROR', "Failed image insert gid={$galleryId} {$filename}");
        }
    }

    // Mark folder processed only when there are no newly discovered images pending.
    // This preserves idempotency while still allowing future new files to be imported.
    $processedState['folders'][$folderRel] = [
        'folder_abs' => $folderAbs,
        'folder_name' => $folderName,
        'gallery_id' => $galleryId,
        'gallery_created' => $createdGallery,
        'imported_images_last_run' => $newCount,
        'skipped_duplicates_last_run' => $dupCount,
        'last_run_at' => current_time('mysql'),
    ];

    if ($newCount > 0 || $createdGallery) {
        $stats['imported_folders']++;
        log_line('INFO', "Folder complete: {$folderRel} (new={$newCount}, dup={$dupCount})");
    } else {
        $stats['skipped_folders']++;
        log_line('SKIPPED', "No new images for {$folderRel}");
    }
}

function find_existing_gallery(string $galleryTable, string $folderRel, string $folderName): ?array
{
    global $wpdb;

    $pathVariants = build_ngg_path_variants($folderRel);

    $whereParts = [];
    $params = [];
    foreach ($pathVariants as $variant) {
        $whereParts[] = 'path = %s';
        $params[] = $variant;
    }

    $whereParts[] = 'name = %s';
    $params[] = sanitize_title($folderName);

    $whereParts[] = 'title = %s';
    $params[] = $folderName;

    $sql = "SELECT gid, path, name, title FROM {$galleryTable} WHERE " . implode(' OR ', $whereParts) . ' ORDER BY gid ASC LIMIT 1';
    $prepared = $wpdb->prepare($sql, $params);
    $row = $wpdb->get_row($prepared, ARRAY_A);

    return is_array($row) ? $row : null;
}

function build_ngg_path_variants(string $folderRel): array
{
    $trimmed = trim($folderRel);
    $noLead = ltrim($trimmed, '/');
    $withLead = '/' . $noLead;
    $withTrail = rtrim($withLead, '/') . '/';

    return array_values(array_unique([
        $withLead,
        $withTrail,
        $noLead,
        rtrim($noLead, '/') . '/',
        'wp-content/' . preg_replace('#^wp-content/#', '', $noLead),
        '/wp-content/' . preg_replace('#^wp-content/#', '', $noLead),
    ]));
}

function create_gallery_record(string $galleryTable, string $folderRel, string $folderName): int
{
    global $wpdb;

    $columns = $wpdb->get_col("SHOW COLUMNS FROM {$galleryTable}", 0);
    if (!is_array($columns) || count($columns) === 0) {
        return 0;
    }

    $slug = sanitize_title($folderName);
    $path = rtrim($folderRel, '/') . '/';

    $data = [];
    if (in_array('name', $columns, true)) {
        $data['name'] = $slug;
    }
    if (in_array('title', $columns, true)) {
        $data['title'] = $folderName;
    }
    if (in_array('path', $columns, true)) {
        $data['path'] = $path;
    }
    if (in_array('pageid', $columns, true)) {
        $data['pageid'] = 0;
    }
    if (in_array('previewpic', $columns, true)) {
        $data['previewpic'] = 0;
    }
    if (in_array('author', $columns, true)) {
        $data['author'] = (int) get_current_user_id();
    }
    if (in_array('slug', $columns, true)) {
        $data['slug'] = $slug;
    }
    if (in_array('galdesc', $columns, true)) {
        $data['galdesc'] = '';
    }
    if (in_array('extras_post_id', $columns, true)) {
        $data['extras_post_id'] = 0;
    }
    if (in_array('date_created', $columns, true)) {
        $data['date_created'] = current_time('mysql');
    }

    $ok = $wpdb->insert($galleryTable, $data);
    if ($ok === false) {
        return 0;
    }

    return (int) $wpdb->insert_id;
}

function load_gallery_filenames(string $pictureTable, int $galleryId): array
{
    global $wpdb;

    $rows = $wpdb->get_col(
        $wpdb->prepare("SELECT filename FROM {$pictureTable} WHERE galleryid = %d", $galleryId)
    );

    $out = [];
    if (is_array($rows)) {
        foreach ($rows as $filename) {
            $out[strtolower((string) $filename)] = true;
        }
    }
    return $out;
}

function list_image_files(string $folderAbs): array
{
    $entries = scandir($folderAbs);
    if ($entries === false) {
        return [];
    }

    $exts = ['jpg', 'jpeg', 'png', 'webp', 'gif'];
    $files = [];

    foreach ($entries as $entry) {
        if ($entry === '.' || $entry === '..') {
            continue;
        }
        $full = wp_normalize_path($folderAbs . '/' . $entry);
        if (!is_file($full)) {
            continue;
        }
        $ext = strtolower(pathinfo($entry, PATHINFO_EXTENSION));
        if (in_array($ext, $exts, true)) {
            $files[] = $full;
        }
    }

    sort($files);
    return $files;
}

function insert_picture_record(string $pictureTable, int $galleryId, string $filename): bool
{
    global $wpdb;

    $columns = $wpdb->get_col("SHOW COLUMNS FROM {$pictureTable}", 0);
    if (!is_array($columns) || count($columns) === 0) {
        return false;
    }

    $slug = sanitize_title(pathinfo($filename, PATHINFO_FILENAME));

    $data = [];
    if (in_array('galleryid', $columns, true)) {
        $data['galleryid'] = $galleryId;
    }
    if (in_array('filename', $columns, true)) {
        $data['filename'] = $filename;
    }
    if (in_array('image_slug', $columns, true)) {
        $data['image_slug'] = $slug;
    }
    if (in_array('alttext', $columns, true)) {
        $data['alttext'] = '';
    }
    if (in_array('description', $columns, true)) {
        $data['description'] = '';
    }
    if (in_array('sortorder', $columns, true)) {
        $data['sortorder'] = 0;
    }
    if (in_array('exclude', $columns, true)) {
        $data['exclude'] = 0;
    }
    if (in_array('imagedate', $columns, true)) {
        $data['imagedate'] = current_time('mysql');
    }
    if (in_array('post_id', $columns, true)) {
        $data['post_id'] = 0;
    }
    if (in_array('meta_data', $columns, true)) {
        $data['meta_data'] = '';
    }

    if (!isset($data['galleryid'], $data['filename'])) {
        return false;
    }

    $ok = $wpdb->insert($pictureTable, $data);
    return $ok !== false;
}

function load_processed_state(string $optionKey, string $processedJson): array
{
    $state = [
        'folders' => [],
        'updated_at' => current_time('mysql'),
    ];

    $opt = get_option($optionKey, null);
    if (is_array($opt) && isset($opt['folders']) && is_array($opt['folders'])) {
        $state = $opt;
    }

    if (is_file($processedJson)) {
        $raw = file_get_contents($processedJson);
        if (is_string($raw) && $raw !== '') {
            $json = json_decode($raw, true);
            if (is_array($json) && isset($json['folders']) && is_array($json['folders'])) {
                // Merge JSON fallback into option state, option state wins.
                $state['folders'] = array_merge($json['folders'], $state['folders']);
            }
        }
    }

    if (!isset($state['folders']) || !is_array($state['folders'])) {
        $state['folders'] = [];
    }

    return $state;
}

function save_processed_state(string $optionKey, string $processedJson, array &$state): void
{
    $state['updated_at'] = current_time('mysql');

    update_option($optionKey, $state, false);

    $dir = dirname($processedJson);
    if (!is_dir($dir)) {
        wp_mkdir_p($dir);
    }

    $json = wp_json_encode($state, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES);
    if (is_string($json)) {
        file_put_contents($processedJson, $json . "\n");
    }
}

function log_line(string $level, string $message): void
{
    $ts = date('Y-m-d H:i:s');
    fwrite(STDOUT, "[{$ts}] {$level} {$message}\n");
}
