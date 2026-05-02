import os
import re
import threading
import time
import requests
import urllib.parse
import json
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler


MAX_DESCRIPTION_LINE_NUM = int(os.environ.get("MAX_DESCRIPTION_LINE_NUM", '5'))
MAX_DESCRIPTION_LENGTH = int(os.environ.get("MAX_DESCRIPTION_LENGTH", '500'))
FAVICON_FILENAME = os.environ.get("FAVICON_FILENAME", 'favicon.ico')

TOKEN_SAVE_PATH = './data/token.json'
ADMIN_CACHE_TTL = 3600

# channel_id -> { project_url -> access_token }
channel_to_gitlab_tokens_map = {}
lock_channel_to_gitlab_tokens_map = threading.Lock()

token_save_dir = os.path.dirname(TOKEN_SAVE_PATH)
if not os.path.isdir(token_save_dir):
    os.makedirs(token_save_dir)

if os.path.isfile(TOKEN_SAVE_PATH):
    with open(TOKEN_SAVE_PATH, "r") as file:
        channel_to_gitlab_tokens_map = json.load(file)

app = App(
    token=os.environ.get("SLACK_BOT_TOKEN")
)


# user_id -> (is_admin, timestamp)
_admin_cache = {}
_admin_cache_lock = threading.Lock()


def is_admin_user(user_id):
    now = time.time()
    with _admin_cache_lock:
        cached = _admin_cache.get(user_id)
        if cached and now - cached[1] < ADMIN_CACHE_TTL:
            return cached[0]

    resp = app.client.users_info(user=user_id)
    is_admin = resp.data['ok'] and resp.data['user'].get('is_admin', False)

    with _admin_cache_lock:
        _admin_cache[user_id] = (is_admin, now)

    return is_admin


def atomic_save(path, data):
    new_path = path + '.new'
    with open(new_path, "w") as f:
        f.write(data)
    os.replace(new_path, path)


def truncate(string, line_count, length, ellipsis='...'):
    lines = string.split('\n')
    truncated = len(lines[line_count:]) > 0
    lines = lines[:line_count]
    string_truncated = '\n'.join(lines)
    truncated = truncated or string[length:] != ''
    return string_truncated[:length] + (ellipsis if truncated else '')


def parse_gitlab_url(url):
    """GitLab URLを解析してプロジェクトURL、リソース種別、IDなどを抽出する。
    対応パターン:
      - /<project_path>/-/work_items/<iid>
      - /<project_path>/-/work_items/<iid>#note_<note_id>
      - /<project_path>/-/merge_requests/<iid>
      - /<project_path>/-/merge_requests/<iid>#note_<note_id>
    """
    parsed = urllib.parse.urlparse(url)
    path = parsed.path

    separator_idx = path.find('/-/')
    if separator_idx == -1:
        return None

    project_path = path[:separator_idx].lstrip('/')
    resource_path = path[separator_idx + 3:]

    if not project_path or not resource_path:
        return None

    wiki_match = re.match(r'^wikis/(.+)', resource_path)
    if wiki_match:
        wiki_slug = wiki_match.group(1)
        base_url = f"{parsed.scheme}://{parsed.netloc}"
        project_url = f"{base_url}/{project_path}"
        return {
            'project_url': project_url,
            'base_url': base_url,
            'project_path': project_path,
            'resource_type': 'wikis',
            'wiki_slug': wiki_slug,
            'heading_anchor': urllib.parse.unquote(parsed.fragment) if parsed.fragment else None,
        }

    match = re.match(r'^(work_items|merge_requests)/(\d+)', resource_path)
    if not match:
        return None

    resource_type = match.group(1)
    resource_iid = match.group(2)

    note_id = None
    if parsed.fragment:
        note_match = re.match(r'^note_(\d+)$', parsed.fragment)
        if note_match:
            note_id = note_match.group(1)

    base_url = f"{parsed.scheme}://{parsed.netloc}"
    project_url = f"{base_url}/{project_path}"

    return {
        'project_url': project_url,
        'base_url': base_url,
        'project_path': project_path,
        'resource_type': resource_type,
        'resource_iid': resource_iid,
        'note_id': note_id,
    }


def find_token_for_url(channel_id, project_url):
    with lock_channel_to_gitlab_tokens_map:
        tokens = channel_to_gitlab_tokens_map.get(channel_id, {})
        return tokens.get(project_url)


def gitlab_api_get(base_url, endpoint, token):
    headers = {'PRIVATE-TOKEN': token}
    url = f"{base_url}/api/v4/{endpoint}"
    req = requests.Request('GET', url, headers=headers)
    prepared = req.prepare()
    prepared.url = url  # requestsのURL正規化で%2Fがデコードされるのを防ぐ
    resp = requests.Session().send(prepared)
    if resp.status_code != 200:
        return None
    return resp.json()


def generate_blocks(url, title, icon_url, description):
    blocks = {
        "blocks": [{
            "type": "context",
            "elements": [
                {
                    "type": "image",
                    "image_url": icon_url,
                    "alt_text": "GitLab"
                },
                {
                    "type": "mrkdwn",
                    "text": "<{}|*{}*>".format(url, title)
                }
            ]
        }]
    }

    if description and description != '':
        blocks["blocks"][0]["elements"].append({
            "type": "plain_text",
            "text": description,
            "emoji": True
        })

    return blocks


def generate_issue_blocks(url, parsed, token):
    encoded_path = urllib.parse.quote(parsed['project_path'], safe='')

    issue = gitlab_api_get(
        parsed['base_url'],
        f"projects/{encoded_path}/issues/{parsed['resource_iid']}",
        token
    )
    if not issue:
        return None

    icon_url = urllib.parse.urljoin(parsed['base_url'], f'/assets/{FAVICON_FILENAME}')

    if parsed['note_id']:
        note = gitlab_api_get(
            parsed['base_url'],
            f"projects/{encoded_path}/issues/{parsed['resource_iid']}/notes/{parsed['note_id']}",
            token
        )
        if note:
            title = 'Issue #{}: {} - {}'.format(
                issue['iid'],
                issue['title'],
                parsed['project_path']
            )
            description = truncate(
                note['body'].replace('\r\n', '\n'),
                MAX_DESCRIPTION_LINE_NUM, MAX_DESCRIPTION_LENGTH
            )
            return generate_blocks(url, title, icon_url, description)

    title = 'Issue #{}: {} - {}'.format(
        issue['iid'],
        issue['title'],
        parsed['project_path']
    )
    description = truncate(
        (issue.get('description') or '').replace('\r\n', '\n'),
        MAX_DESCRIPTION_LINE_NUM, MAX_DESCRIPTION_LENGTH
    )
    return generate_blocks(url, title, icon_url, description)


def extract_heading_section(content, anchor):
    """Markdownコンテンツから指定アンカーに対応する見出しとその本文を抽出する。"""
    lines = content.split('\n')
    heading_text = None
    section_lines = []
    heading_level = None
    in_section = False

    for line in lines:
        heading_match = re.match(r'^(#{1,6})\s+(.+)', line)
        if heading_match:
            level = len(heading_match.group(1))
            text = heading_match.group(2).strip()
            line_anchor = re.sub(r'[^\w\s-]', '', text.lower())
            line_anchor = re.sub(r'[\s]+', '-', line_anchor)

            if in_section:
                if level <= heading_level:
                    break
                section_lines.append(line)
            elif line_anchor == anchor:
                heading_text = text
                heading_level = level
                in_section = True
        elif in_section:
            section_lines.append(line)

    if heading_text is None:
        return None, None

    body = '\n'.join(section_lines).strip()
    return heading_text, body


def generate_wiki_blocks(url, parsed, token):
    encoded_path = urllib.parse.quote(parsed['project_path'], safe='')
    wiki_slug = parsed['wiki_slug']

    wiki_page = gitlab_api_get(
        parsed['base_url'],
        f"projects/{encoded_path}/wikis/{urllib.parse.quote(urllib.parse.unquote(wiki_slug), safe='')}",
        token
    )
    if not wiki_page:
        return None

    icon_url = urllib.parse.urljoin(parsed['base_url'], f'/assets/{FAVICON_FILENAME}')
    page_title = wiki_page.get('title', wiki_slug)

    if parsed['heading_anchor']:
        heading_text, section_body = extract_heading_section(
            wiki_page.get('content', ''),
            parsed['heading_anchor']
        )
        if heading_text:
            title = '{} / {} - {}'.format(page_title, heading_text, parsed['project_path'])
            description = truncate(
                (section_body or '').replace('\r\n', '\n'),
                MAX_DESCRIPTION_LINE_NUM, MAX_DESCRIPTION_LENGTH
            )
            return generate_blocks(url, title, icon_url, description)

    title = '{} - {}'.format(page_title, parsed['project_path'])
    description = truncate(
        (wiki_page.get('content') or '').replace('\r\n', '\n'),
        MAX_DESCRIPTION_LINE_NUM, MAX_DESCRIPTION_LENGTH
    )
    return generate_blocks(url, title, icon_url, description)


def generate_merge_request_blocks(url, parsed, token):
    encoded_path = urllib.parse.quote(parsed['project_path'], safe='')

    mr = gitlab_api_get(
        parsed['base_url'],
        f"projects/{encoded_path}/merge_requests/{parsed['resource_iid']}",
        token
    )
    if not mr:
        return None

    icon_url = urllib.parse.urljoin(parsed['base_url'], f'/assets/{FAVICON_FILENAME}')

    if parsed['note_id']:
        note = gitlab_api_get(
            parsed['base_url'],
            f"projects/{encoded_path}/merge_requests/{parsed['resource_iid']}/notes/{parsed['note_id']}",
            token
        )
        if note:
            title = 'MR !{}: {} - {}'.format(
                mr['iid'],
                mr['title'],
                parsed['project_path']
            )
            description = truncate(
                note['body'].replace('\r\n', '\n'),
                MAX_DESCRIPTION_LINE_NUM, MAX_DESCRIPTION_LENGTH
            )
            return generate_blocks(url, title, icon_url, description)

    title = 'MR !{}: {} - {}'.format(
        mr['iid'],
        mr['title'],
        parsed['project_path']
    )
    description = truncate(
        (mr.get('description') or '').replace('\r\n', '\n'),
        MAX_DESCRIPTION_LINE_NUM, MAX_DESCRIPTION_LENGTH
    )
    return generate_blocks(url, title, icon_url, description)


def get_channel_name(client, channel_id):
    try:
        resp = client.conversations_info(channel=channel_id)
        if resp.data['ok']:
            return '#' + resp.data['channel']['name']
    except Exception:
        pass
    return None


@app.event("link_shared")
def handle_link_shared_events(body, ack, client):
    ack()

    channel_id = body["event"]["channel"]
    unfurls = {}

    for link in body["event"]["links"]:
        url = link["url"]
        parsed = parse_gitlab_url(url)
        if not parsed:
            continue

        token = find_token_for_url(channel_id, parsed['project_url'])
        if not token:
            continue

        blocks = None
        if parsed['resource_type'] == 'work_items':
            blocks = generate_issue_blocks(url, parsed, token)
        elif parsed['resource_type'] == 'merge_requests':
            blocks = generate_merge_request_blocks(url, parsed, token)
        elif parsed['resource_type'] == 'wikis':
            blocks = generate_wiki_blocks(url, parsed, token)

        if blocks:
            unfurls[url] = blocks

    if unfurls:
        client.chat_unfurl(
            channel=channel_id,
            ts=body["event"]["message_ts"],
            unfurls=unfurls
        )


@app.command("/gitlab_register_token")
def gitlab_register_token(ack, respond, command):
    ack()

    user_id = command['user_id']
    if not is_admin_user(user_id):
        respond("あなたは管理ユーザーではありません")
        return

    channel_id = command['channel_id']
    text = command['text'].strip()
    parts = text.split()

    if len(parts) != 2:
        respond("使い方: /gitlab_register_token <プロジェクトURL> <アクセストークン>")
        return

    project_url = parts[0].rstrip('/')
    access_token = parts[1]

    with lock_channel_to_gitlab_tokens_map:
        if channel_id not in channel_to_gitlab_tokens_map:
            channel_to_gitlab_tokens_map[channel_id] = {}
        channel_to_gitlab_tokens_map[channel_id][project_url] = access_token
        json_string = json.dumps(channel_to_gitlab_tokens_map, indent=4)
        atomic_save(TOKEN_SAVE_PATH, json_string)

    respond(f"トークンを登録しました: {project_url}")


@app.command("/gitlab_unregister_token")
def gitlab_unregister_token(ack, respond, command):
    ack()

    user_id = command['user_id']
    if not is_admin_user(user_id):
        respond("あなたは管理ユーザーではありません")
        return

    channel_id = command['channel_id']
    project_url = command['text'].strip().rstrip('/')

    if not project_url:
        respond("使い方: /gitlab_unregister_token <プロジェクトURL>")
        return

    with lock_channel_to_gitlab_tokens_map:
        tokens = channel_to_gitlab_tokens_map.get(channel_id, {})
        if project_url in tokens:
            del tokens[project_url]
            if not tokens:
                del channel_to_gitlab_tokens_map[channel_id]
            json_string = json.dumps(channel_to_gitlab_tokens_map, indent=4)
            atomic_save(TOKEN_SAVE_PATH, json_string)
            respond(f"トークンを削除しました: {project_url}")
        else:
            respond(f"このチャンネルにはそのプロジェクトURLのトークンが登録されていません: {project_url}")


@app.command("/gitlab_list_registered_tokens")
def gitlab_list_registered_tokens(ack, respond, command):
    ack()

    user_id = command['user_id']
    if not is_admin_user(user_id):
        respond("あなたは管理ユーザーではありません")
        return

    respond_text = ''
    with lock_channel_to_gitlab_tokens_map:
        for channel_id, tokens in channel_to_gitlab_tokens_map.items():
            for project_url, token in tokens.items():
                respond_text += f'<#{channel_id}>: {project_url} -> `{token}`\n'

    if respond_text:
        respond(respond_text.rstrip('\n'))
    else:
        respond("トークンが登録されていません")


if __name__ == "__main__":
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
