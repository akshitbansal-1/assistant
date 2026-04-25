# Connector Setup Guide

How to create OAuth credentials for each provider and add them to `.env`.

> **Port note:** All redirect URIs below use `http://localhost:3000`. If you change `APP_PORT`, update the redirect URIs in both `.env` and each provider's developer console.

---

## Google (Gmail)

**Required `.env` keys**
```
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
GOOGLE_REDIRECT_URI=http://localhost:3000/api/v1/oauth/gmail/callback
```

**Steps**

1. Go to [Google Cloud Console](https://console.cloud.google.com/) → select or create a project.
2. **APIs & Services → Enabled APIs → + Enable APIs and Services** → search **Gmail API** → click **Enable**. *(This is required even if OAuth works — the OAuth consent screen and Gmail API are separate.)*
3. **APIs & Services → OAuth consent screen**
   - User Type: **External** (for personal accounts) or **Internal** (G Suite).
   - Fill in app name, support email, developer email.
   - Add scopes: `gmail.readonly`, `userinfo.email`.
   - Add your Google account as a **test user** while the app is in testing mode.
4. **APIs & Services → Credentials → Create Credentials → OAuth client ID**
   - Application type: **Web application**
   - Authorized redirect URIs: `http://localhost:3000/api/v1/oauth/gmail/callback`
5. Copy the **Client ID** and **Client secret** into `.env`.

**Scopes used:** `gmail.readonly`, `userinfo.email`

---

## Slack

**Required `.env` keys**
```
SLACK_CLIENT_ID=
SLACK_CLIENT_SECRET=
SLACK_REDIRECT_URI=http://localhost:3000/api/v1/oauth/slack/callback
```

**Steps**

1. Go to [Slack API → Your Apps](https://api.slack.com/apps) → **Create New App → From scratch**.
2. Name the app and pick your development workspace.
3. **OAuth & Permissions → Redirect URLs** → add `http://localhost:3000/api/v1/oauth/slack/callback` → **Save URLs**.
4. **OAuth & Permissions → Bot Token Scopes** → add:
   - `channels:history`
   - `groups:history`
   - `im:history`
   - `mpim:history`
   - `users:read`
   - `chat:write`
5. **Basic Information** → copy **Client ID** and **Client Secret** into `.env`.

**Scopes used:** `channels:history`, `groups:history`, `im:history`, `mpim:history`, `users:read`, `chat:write`

---

## Notion

**Required `.env` keys**
```
NOTION_CLIENT_ID=
NOTION_CLIENT_SECRET=
NOTION_REDIRECT_URI=http://localhost:3000/api/v1/oauth/notion/callback
```

**Steps**

1. Go to [Notion Integrations](https://www.notion.so/profile/integrations) → **New integration**.
2. Name it, pick the associated workspace.
3. Integration type: **Public** (required for OAuth — allows users outside your workspace to connect).
4. **OAuth Domain & URIs** → add `http://localhost:3000/api/v1/oauth/notion/callback` as a redirect URI.
5. Copy **OAuth client ID** and **OAuth client secret** into `.env`.

> Notion doesn't use explicit scopes in the authorization URL; access is granted per-page when the user connects.

---

## Jira (Atlassian)

**Required `.env` keys**
```
JIRA_CLIENT_ID=
JIRA_CLIENT_SECRET=
JIRA_REDIRECT_URI=http://localhost:3000/api/v1/oauth/jira/callback
```

**Steps**

1. Go to [Atlassian Developer Console](https://developer.atlassian.com/console/myapps/) → **Create** → **OAuth 2.0 integration**.
2. Name the app.
3. **Authorization** tab → add callback URL: `http://localhost:3000/api/v1/oauth/jira/callback`.
4. **Permissions** tab → **Jira API** → enable `read:jira-work` and `read:jira-user`.
5. **Settings** tab → copy **Client ID** and **Secret** into `.env`.

**Scopes used:** `read:jira-work`, `read:jira-user`, `offline_access`

---

## Common issues

| Error | Cause | Fix |
|-------|-------|-----|
| `redirect_uri_mismatch` | The redirect URI in `.env` doesn't match what's registered in the provider console | Update the redirect URI in the console to match `.env` exactly, including the port |
| `invalid_client` | Wrong client ID or secret | Re-copy the credentials from the provider console |
| `access_denied` | User cancelled, or Google test mode with no test user | Add your Google account as a test user in the OAuth consent screen |
| Notion 401 on token exchange | Using a *Private* integration instead of *Public* | Recreate as a Public integration |
