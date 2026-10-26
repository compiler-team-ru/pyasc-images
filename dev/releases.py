"""Keep development evidence out of GitHub's stable base-release pointer."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ci'))
import pipeline as base


def api(method, path, data):
    result = subprocess.run(['gh', 'api', '--method', method, path, '--input', '-'],
                            input=json.dumps(data), text=True, capture_output=True, check=True)
    return json.loads(result.stdout)


def reconcile():
    releases = base.pages(f'repos/{base.REPO}/releases')
    for release in releases:
        if release['tag_name'].startswith(('dev-tests-', 'dev-matrix-')) and not release['prerelease']:
            api('PATCH', f'repos/{base.REPO}/releases/{release["id"]}',
                {'prerelease': True, 'make_latest': 'false'})
    # Select by completed report, not by a release's name or creation time alone.
    for release in sorted(releases, key=lambda r: r['published_at'] or '', reverse=True):
        if not release['tag_name'].startswith('matrix-') or release['draft']:
            continue
        if not any(a['name'] == 'matrix.json' for a in release['assets']):
            continue
        report = base.asset_json(release, 'matrix.json')
        cells, manifests = report.get('cells', []), report.get('manifests', [])
        if (len(cells) == 32 and all(c['status'] in ('pass', 'incompatible') for c in cells)
                and len(manifests) == sum(c['status'] == 'pass' for c in cells) // 2
                and manifests and all(m['status'] == 'pass' for m in manifests)):
            api('PATCH', f'repos/{base.REPO}/releases/{release["id"]}',
                {'prerelease': False, 'make_latest': 'true'})
            print('Stable base release:', release['tag_name'])
            return
    raise RuntimeError('No complete base matrix release found')


def reserve(report_tag, qualification_recipe):
    """Reserve prereleases for already-running jobs using the previous publisher."""
    assert len(qualification_recipe) == 64 and all(c in '0123456789abcdef' for c in qualification_recipe)
    releases = base.pages(f'repos/{base.REPO}/releases')
    report_release = next(r for r in releases if r['tag_name'] == report_tag)
    report = base.asset_json(report_release, 'dev-matrix.json')
    existing = {r['tag_name'] for r in releases}
    for cell in report['cells']:
        if cell['status'] != 'published':
            continue
        tag = 'dev-tests-' + cell['id'] + '-' + qualification_recipe[:12]
        if tag not in existing:
            api('POST', f'repos/{base.REPO}/releases',
                {'tag_name': tag, 'target_commitish': os.environ.get('GITHUB_SHA', 'main'),
                 'name': tag, 'body': 'Reserved for source-pinned qualification checkpoints; no suite pass is implied.',
                 'prerelease': True, 'make_latest': 'false'})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reserve-report')
    parser.add_argument('--qualification-recipe')
    args = parser.parse_args()
    if args.reserve_report:
        assert args.qualification_recipe
        reserve(args.reserve_report, args.qualification_recipe)
    reconcile()
