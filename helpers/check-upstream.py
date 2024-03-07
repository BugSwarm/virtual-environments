from collections import deque
import json
import re
import sys
from typing import Any

try:
    import git
    import hcl2
    import jq
except ImportError:
    print('Required packages missing.')
    print('Run "pip3 install GitPython python-hcl2 jq" to install packages.')
    sys.exit(1)


repo: git.Repo = None


def file_at_commit(commit: git.Commit, path: str) -> str:
    return commit.tree.join(path).data_stream.read().decode('utf-8')


def fetch_upstream_changes():
    # repo = git.Repo('.')
    if 'upstream' not in repo.remotes:
        repo.create_remote(
            'upstream', 'https://github.com/actions/runner-images.git')
    upstream = repo.remotes.upstream
    upstream.fetch()


def get_latest_releases() -> 'tuple[git.TagReference, git.TagReference]':
    # repo = git.Repo('.')
    tagnames = reversed(repo.git.tag('--sort', 'authordate').splitlines())
    ubuntu_tagname = next(name for name in tagnames if name.startswith('ubuntu22/'))
    bugswarm_tagname = next(name for name in tagnames if name.startswith('bugswarm/'))
    return repo.tag(ubuntu_tagname), repo.tag(bugswarm_tagname)


def get_changed_files(first: git.Commit, second: git.Commit) -> 'list[str]':
    return [diff.a_path for diff in first.diff(second)]


def get_scripts_in_template(commit: git.Commit, hcl_path: str) -> 'list[str]':
    """
    Gets all of the scripts mentioned in the given HCL file at the given commit.
    """
    file_contents = file_at_commit(commit, hcl_path)
    obj = hcl2.loads(file_contents)

    steps = obj['build'][0]['provisioner']
    scripts_used = []
    for step in steps:
        if 'shell' not in step:
            continue
        if 'script' in step['shell']:
            scripts_used.append(step['shell']['script'])
        elif 'scripts' in step['shell']:
            scripts_used.extend(step['shell']['scripts'])

    paths_in_repo = [path.replace(
        '${path.root}/..', 'images/ubuntu') for path in scripts_used]
    return paths_in_repo


def get_helper_scripts(commit: git.Commit) -> 'list[str]':
    tree = commit.tree / 'images/ubuntu/scripts/helpers'
    return [item.path for item in tree.traverse()]


def extract_jq_queries(commit: git.Commit, script_paths: 'list[str]') -> 'list[str]':
    toolset_queries = []
    for script in script_paths:
        contents = file_at_commit(commit, script)

        # Matches $(get_toolset_value <foo>), capturing the contents of <foo> (stripping quotes if present).
        # NOTE: Assumes that shell characters are escaped using quotes (not backslashes), and that the $(...) is not
        # surrounded by double quotes. As of 2024/03/01, this holds true for this repo.
        matches = re.finditer(
            r'\$\(get_toolset_value (?:\'(.*)\'|"(.*)"|(.*?))\)', contents)

        for match in matches:
            query = next(group for group in match.groups() if group is not None)
            toolset_queries.append(query)

    return toolset_queries


def compare_toolset_values(
    commit_1: git.Commit,
    commit_2: git.Commit,
    toolset_json_path: str,
    queries: 'list[str]'
) -> 'list[tuple[str, Any, Any]]':

    toolset_1 = json.loads(file_at_commit(commit_1, toolset_json_path))
    toolset_2 = json.loads(file_at_commit(commit_2, toolset_json_path))

    differences = []

    for query in queries:
        result_1 = jq.compile(query).input_value(toolset_1).all()
        result_2 = jq.compile(query).input_value(toolset_2).all()

        if result_1 != result_2:
            differences.append((query, result_1, result_2))

    return differences


def main():
    global repo
    repo = git.Repo('.')

    ubuntu22_hcl = 'images/ubuntu/templates/ubuntu-22.04.pkr.hcl'
    ubuntu20_hcl = 'images/ubuntu/templates/ubuntu-20.04.pkr.hcl'
    ubuntu22_toolset = 'images/ubuntu/toolsets/toolset-2204.json'
    ubuntu20_toolset = 'images/ubuntu/toolsets/toolset-2004.json'

    # 0 = no action needed; 1 = check diffs; 2 = should rebuild
    recommendation = 0

    print('Fetching upstream changes...')
    fetch_upstream_changes()
    ubuntu_tag, bugswarm_tag = get_latest_releases()

    # Get the 3-dot diff between the bugswarm release and the ubuntu release
    # (i.e., changes to the ubuntu release since the bugswarm release was forked)
    merge_base = repo.merge_base(bugswarm_tag.commit, ubuntu_tag.commit)[0]
    changed_files = set(get_changed_files(merge_base, ubuntu_tag.commit))

    # Get all the scripts used in the packer HCL templates, plus every helper script.
    scripts_used = set(
        get_scripts_in_template(bugswarm_tag.commit, ubuntu22_hcl) +
        get_scripts_in_template(bugswarm_tag.commit, ubuntu20_hcl) +
        get_helper_scripts(bugswarm_tag.commit)
    )

    # Check whether any scripts were changed upstream
    changed_scripts = scripts_used & changed_files
    if changed_scripts:
        recommendation = max(recommendation, 1)
        print('\n⚠️ The following SCRIPTS were changed upstream:')
        for script in sorted(changed_scripts):
            print(f'    {script}')
        print('Check their diffs to see whether the changes warrant a rebuild.')

    # Check whether the HCL templates were changed upstream
    changed_hcls = {ubuntu22_hcl, ubuntu20_hcl} & changed_files
    if changed_hcls:
        recommendation = max(recommendation, 1)
        print('\n⚠️ The following TEMPLATES were changed upstream:')
        for hcl_file in sorted(changed_hcls):
            print(f'    {hcl_file}')
        print('Check their diffs to see whether the changes warrant a rebuild.')

    # Get all `jq` queries used in each script.
    used_toolset_queries = extract_jq_queries(bugswarm_tag.commit, scripts_used)
    changed_toolsets = {ubuntu20_toolset, ubuntu22_toolset} & changed_files

    # Check the modified toolset JSONs to ensure that none of the `jq`
    # queries yield different results.
    for toolset_json in sorted(changed_toolsets):
        toolset_diffs = compare_toolset_values(
            merge_base, ubuntu_tag.commit, toolset_json, used_toolset_queries)

        if toolset_diffs:
            recommendation = max(recommendation, 2)  # rebuild
            print(f'\n⛔ The following values were changed in {toolset_json}:')
            for query, original, updated in toolset_diffs:
                print(f'    {query}')
                print(f'        - {original}')
                print(f'        + {updated}')

    print()
    print('=== RESULTS ===')
    if recommendation == 0:
        print('No noteworthy changes upstream.')
        print('There is no reason to rebuild the images.')
    elif recommendation == 1:
        print('There are some changes upstream that could be significant.')
        print('Check the diffs from upstream before deciding whether to rebuild the images:')
        print(f'  git diff {bugswarm_tag.name}...{ubuntu_tag.name} -- <paths> ')
    elif recommendation == 2:
        print('There are significant changes upstream.')
        print("It's probably a good idea to rebuild the images.")


if __name__ == '__main__':
    sys.exit(main())
