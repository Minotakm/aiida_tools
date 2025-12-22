# AiiDA Inspector Data Files

This directory contains standalone data files used by the AiiDA Error Inspector TUI application.

## Files

### `tags.json`
Stores the mapping between AiiDA workchain PKs and their assigned error tags.
- **Format**: `{"workchain_pk": "tag_name", ...}`
- **Editable**: Yes, you can manually add or remove tags

### `patterns.json`
Contains error patterns for automatic tagging of failed calculations.
- **Format**: `{"tag_name": {"filename": "output_file", "pattern": "error_pattern"}, ...}`
- **Editable**: Yes, you can add custom error patterns
- **Example**: `{"bfgs_error": {"filename": "aiida.out", "pattern": "BFGS history already reset"}}`

### `categorized.json`
Tracks which workchains have been categorized/tagged (to avoid re-processing).
- **Format**: `[workchain_pk1, workchain_pk2, ...]`
- **Editable**: Yes, but be careful - removing entries will cause re-categorization

## Usage

These files are automatically managed by the AiiDA Error Inspector TUI, but you can:
- Open and inspect them directly
- Manually edit patterns to add new error categories
- Share patterns with colleagues
- Version control them to track error categorization over time
