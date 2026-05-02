# slack-unfurl-gitlab

## 概要
Slackでアクセスにトークンが必要なGitLabのリンクを展開するBot。  
Issue/マージリクエストの場合はタイトルと内容の一部を表示する。  
コメントへのリンクの場合はコメントの内容を表示する。  
ソケットモードで動作するのでWebサーバーが不要。

## インストール方法

### 1. docker-compose.ymlの用意
1. `docker-compose.yml.template` をコピーして `docker-compose.yml` を用意する

設定値のうち `SLACK_*` は `2. Slack Appの作成` で生成される値なので `docker-compose.yml` にコピーすること。

### 2. Slack Appの作成

1. https://api.slack.com/apps の Create New App からアプリ作成
2. 左メニュー Socket Mode を開き、Enable Socket ModeをOnに変更、App Tokenをクリップボードにコピー (`SLACK_APP_TOKEN`)
3. 左メニュー OAuth & Permissions を開き、 Scopes で以下を追加
    - `links:write` (必須) - リンク展開に必要
    - `users:read` (必須) - 管理者判定に必要
    - `channels:read` (任意) - `/gitlab_list_registered_tokens` でパブリックチャンネル名を表示するために必要
    - `groups:read` (任意) - `/gitlab_list_registered_tokens` でプライベートチャンネル名を表示するために必要
4. 左メニュー Event Subscriptions を開き、 Enable Events を On に変更
    - App unfurl domains を展開し、 Add Domain で使用するGitLabのドメイン 例 `gitlab.example.com` を入力して Save Changes
5. 左メニュー Slash Commands を開き、以下のコマンドを追加する
    - `/gitlab_register_token` - プロジェクトURLとアクセストークンを登録する
    - `/gitlab_unregister_token` - プロジェクトURLのトークンを削除する
    - `/gitlab_list_registered_tokens` - 登録済みトークン一覧を表示する
6. 左メニュー Install App を開き、 Install App to Workspace -> Allow
7. OAuth Access Token が表示されるのでクリップボードにコピー (`SLACK_BOT_TOKEN`)

### 3. Docker Composeで起動する

## 初期セットアップ方法
使用するチャンネルに対して以下のセットアップが必要

1. Botをチャンネルに招待する
    - これをしなくても動きはするが `/gitlab_list_registered_tokens` でチャンネル名が表示されなくなってしまうのでちゃんと招待すること
2. Slackの管理者権限を持つユーザーで `/gitlab_register_token <GitLabプロジェクトURL> <プロジェクトアクセストークン>` と入力し、そのチャンネルで使用するトークンを登録する
    - 例: `/gitlab_register_token https://gitlab.example.com/group/project glpat-xxxxxxxxxxxxxxxxxxxx`
    - 1つのチャンネルに複数のプロジェクトを登録できる

## 環境変数

| 環境変数名 | 必須 | デフォルト値 | 説明 |
| --- | --- | --- | --- |
| `SLACK_APP_TOKEN` | Yes | - | Slack AppのApp Token (`xapp-`で始まる) |
| `SLACK_BOT_TOKEN` | Yes | - | Slack AppのOAuth Access Token (`xoxb-`で始まる) |
| `MAX_DESCRIPTION_LINE_NUM` | No | `5` | 展開時に表示する説明文の最大行数 |
| `MAX_DESCRIPTION_LENGTH` | No | `500` | 展開時に表示する説明文の最大文字数 |
| `FAVICON_FILENAME` | No | `favicon.ico` | GitLabのfaviconファイル名 (`/assets/` 配下) |

## その他
- `/gitlab_list_registered_tokens` でトークンが登録されているチャンネルとそれに紐づいているプロジェクトURL・トークン一覧が表示できる
- `/gitlab_unregister_token <プロジェクトURL>` で実行したチャンネルの指定プロジェクトに紐づくトークンを削除できる
- チャンネルがアクセスできるプロジェクトの範囲はそのチャンネルに登録するプロジェクトアクセストークンにより制御する設計
    - プロジェクトアクセストークンはGitLabの各プロジェクト設定 > アクセストークン から発行する
    - トークンには `read_api` スコープが必要
