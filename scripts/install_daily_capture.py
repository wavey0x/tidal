"""Small, idempotent Tidal integration with Electro's existing daily mirror."""

BEGIN = '# BEGIN TIDAL NATIVE CAPTURE'
END = '# END TIDAL NATIVE CAPTURE'


def integrate(source):
    if BEGIN in source:
        if source.count(BEGIN) != 1 or source.count(END) != 1 or '"${tidal_excludes[@]}"' not in source:
            raise ValueError('Existing Tidal backup integration is incomplete; inspect before editing')
        return source
    anchor = 'overall_status=0\n'
    rsync = 'rsync -avz --delete "$dir" "$DEST_DIR"'
    if source.count(anchor) != 1 or source.count(rsync) != 1:
        raise ValueError('Daily backup script differs from the inspected route; review the integration')
    addition = '''# BEGIN TIDAL NATIVE CAPTURE
# Capture the coherent DB and exact offline release before the ordinary mirror.
if /usr/local/sbin/tidal-backup >> "$LOG_FILE" 2>&1; then
    log_with_date "Tidal native capture verified."
else
    log_with_date "Tidal native capture failed."
    overall_status=1
fi
# Tidal recovery inputs live in the verified capture; activation never restores.
# These anchored exclusions apply only to the existing /home/wavey mirror.
tidal_excludes=(
    --exclude '/wavey/.tidal/***'
    --exclude '/wavey/tidal-releases/***'
    --exclude '/wavey/tidal-artifacts/***'
    --exclude '/wavey/tidal-runtime/***'
    --exclude '/wavey/tidal-build-*/***'
    --exclude '/wavey/tidal-rehearsal*/***'
    --exclude '/wavey/tidal-test-wheels/***'
)
# END TIDAL NATIVE CAPTURE
'''
    return source.replace(anchor, anchor + addition).replace(rsync,
        'rsync -avz --delete "${tidal_excludes[@]}" "$dir" "$DEST_DIR"')
