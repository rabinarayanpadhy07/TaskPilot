# Authorization model

How TaskPilot decides who can do what. The code is the source of truth:

| Piece | File |
| ----- | ---- |
| Roles, permissions, matrices, role ceilings | `workspaces/roles.py` |
| Resolving a user's access from the database | `workspaces/access.py` |
| DRF integration (membership lookup, object checks) | `workspaces/permissions.py` |
| Membership & invitation rules | `workspaces/services.py` |
| Tests (policy + every API surface) | `tests/` |

The frontend mirrors the matrix in `frontend/src/models/roles.js` **for display
only**. Keep the three in sync.

## Principles

1. **The server decides, every time.** Each protected endpoint authorizes the
   request on the server. Disabled buttons in the UI are a convenience.
2. **Access comes from database rows, never from the request.** A user's role is
   read from `WorkspaceMembership` for `(request.user, workspace in the URL)`.
   Roles, workspace ids or permissions in a request body are ignored
   (`workspace` is read-only on every serializer).
3. **No cross-workspace data.** Every queryset starts from the caller's
   workspace (`self.workspace`) or from `access.visible_projects(membership)`.
   Related ids in a payload (project, assignee, parent, sprint, lead, project
   member) are validated against querysets limited to that scope, so an id
   from another workspace fails exactly like an id that doesn't exist.
4. **Hidden means 404.** Non-members get `404` for a workspace and everything in
   it; private projects (and their issues, sprints, members, reports) are `404`
   for people who can't see them. `403` only means "you can see this, but you
   may not do that".
5. **Deny by default.** `IsAuthenticated` is the global default, and
   `IsWorkspaceMember` rejects any unsafe action that isn't explicitly mapped to
   a permission.

## Workspace roles

| Permission | Owner | Admin | Project Manager | Developer | Viewer |
| ---------- | :---: | :---: | :---: | :---: | :---: |
| `workspace.view`, `members.view`, `reports.view` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `projects.create` | ✓ | ✓ | ✓ | | |
| `members.invite` (invite, list/revoke invitations) | ✓ | ✓ | | | |
| `members.manage` (change roles, remove members) | ✓ | ✓ | | | |
| `workspace.update` | ✓ | ✓ | | | |
| `projects.view_all` (private projects without membership) | ✓ | ✓ | | | |
| `projects.manage_all` (manager in every project) | ✓ | ✓ | | | |
| `projects.delete` | ✓ | ✓ | | | |
| `workspace.delete` | ✓ | | | | |

**Role hierarchy** (rank: Owner 50 > Admin 40 > Project Manager 30 > Developer 20 > Viewer 10):

- Owners can grant any role and manage anyone, including other owners.
- Everyone else can only grant roles **below** their own and only change or
  remove people whose current role is **below** their own (an Admin can't
  promote someone to Admin, or touch an Owner or another Admin).
- Nobody can change their own role. Anyone can leave (`DELETE` their own membership).
- A workspace always keeps at least one Owner: the last Owner can't be demoted,
  removed or leave.
- Removing a member deletes their project memberships in that workspace,
  unassigns their open issues, clears them as project lead and revokes pending
  invitations for their address, in one transaction.

## Project access

Inside a project the effective role is **Manager**, **Contributor** or **Viewer**:

| Project permission | Manager | Contributor | Viewer |
| ------------------ | :---: | :---: | :---: |
| `project.view` (project, issues, sprints, members) | ✓ | ✓ | ✓ |
| `issues.write` (create, edit, move issues) | ✓ | ✓ | |
| `issues.comment` | ✓ | ✓ | |
| `issues.delete` | ✓ | | |
| `sprints.manage` (create, edit, start, complete) | ✓ | | |
| `project.update` (name, status, visibility, lead) | ✓ | | |
| `project.members.manage` | ✓ | | |

`access.effective_project_role(membership, project)` resolves it:

1. Project in another workspace → **none**.
2. Owner / Admin → **Manager** (they hold `projects.manage_all`).
3. Explicit `ProjectMembership` → its role, **capped** by the workspace role
   (ceiling: Project Manager → Manager, Developer → Contributor, Viewer → Viewer).
4. Workspace-visible project → at least the default for the workspace role
   (Project Manager / Developer → Contributor, Viewer → Viewer).
5. Private project without membership → **none** (hidden, `404`).

The highest of (3) and (4) wins. Adding a project member with a role above
their ceiling is rejected (`400`), so the stored role is never misleading.

## Invitations

- Only `members.invite` holders can invite, and only with a role they could grant.
- The token is 256 bits from `secrets.token_urlsafe`; only its SHA-256 hash is
  stored. The raw link is returned once in the create response (and emailed).
- Invitations expire (`INVITATION_TTL_DAYS`, default 7), are single-use, and
  re-inviting an address revokes its previous open invitation.
- `GET /invitations/:token/` is public but minimal (workspace name, invited
  email, role, inviter, expiry) and throttled.
- Accepting requires authentication **and** that the signed-in email matches
  the invited email. Existing members keep their current role.

## Reports

`GET /workspaces/:ws/reports/` needs `reports.view` (every role) and aggregates
only over `access.visible_projects(membership)`. A `project` filter outside that
set is a `404`. So a Developer's numbers never include private projects they
aren't in, and nobody's numbers include another workspace.

## Endpoint map

| Endpoint | Rule |
| -------- | ---- |
| `GET/POST /workspaces/` | Own memberships only / creator becomes Owner |
| `GET/PATCH/DELETE /workspaces/:ws/` | view / `workspace.update` / `workspace.delete` |
| `GET /workspaces/:ws/members/` | `members.view` |
| `PATCH /workspaces/:ws/members/:user/` | `members.manage` + hierarchy + assignable role + last owner |
| `DELETE /workspaces/:ws/members/:user/` | self (leave) or `members.manage` + hierarchy; last owner kept |
| `GET/POST /workspaces/:ws/invitations/`, `DELETE …/:id/` | `members.invite` (+ hierarchy for the invited role) |
| `GET /invitations/:token/`, `POST …/accept/` | public (throttled) / authenticated + matching email |
| `GET/POST /workspaces/:ws/projects/` | visible projects / `projects.create` |
| `PATCH/DELETE /workspaces/:ws/projects/:id/` | `project.update` / `projects.delete` |
| `…/projects/:id/members/[:user/]` | `project.view` to read, `project.members.manage` to change |
| `GET/POST /workspaces/:ws/issues/` | visible projects / `issues.write` in the target project |
| `PATCH/DELETE /workspaces/:ws/issues/:key/` | `issues.write` / `issues.delete` (object-level) |
| `…/projects/:id/sprints/`, `/workspaces/:ws/sprints/:id/[start|complete]/` | `project.view` to read, `sprints.manage` to change |
| `GET /workspaces/:ws/reports/` | `reports.view`, scoped to visible projects |
| `GET/POST/PATCH/DELETE /workspaces/:ws/teams/[:id/]` | `members.view` to read, `members.manage` to change; members must belong to the workspace |
| `POST /workspaces/:ws/issues/:key/move/` | `issues.write` (object-level); neighbours, sprint and parent must be in the same visible project |
| `POST /workspaces/:ws/issues/bulk/` | `issues.write` on **every** issue, else nothing changes (403/404/400) |
| `…/issues/:key/comments/[:id/]` | read: `project.view`; create: `issues.comment`; edit: author; delete: author or `issues.delete` |
| `…/issues/:key/attachments/[:id/]` | read: `project.view`; upload: `issues.write` (10 MB, allow-listed types); delete: uploader or `issues.delete` |
| `GET /attachments/:signed-token/` | No bearer token: an HMAC-signed, 15-minute link bound to attachment + user; access is re-checked at download |
| `GET …/issues/:key/activity/`, `GET /workspaces/:ws/activity/` | `project.view`; history is written only by the server |
| `GET /workspaces/:ws/labels/`, `GET /workspaces/:ws/search/` | Membership; results limited to visible projects |
| `GET /notifications/?workspace=`, `POST /notifications/mark-read/`, `POST /notifications/:id/mark-unread/` | Only the caller's own notifications, in a workspace they belong to; notifications about projects they can no longer see are hidden |

Notifications and @mentions are only created for people who can currently view the
issue's project, so they never disclose private projects.

## Adding a new endpoint

1. Mount it under `/workspaces/<workspace_pk>/` and inherit `WorkspaceScopedMixin`.
2. Add `IsWorkspaceMember` (and `ProjectObjectPermission` for project-scoped objects).
3. Map **every** unsafe action in `workspace_perms` (a `WorkspacePerm`, or
   `PROJECT_LEVEL` plus `project_perms` / `access.assert_project_perm`).
4. Build the queryset from `self.workspace` or `access.visible_projects(...)`.
5. Limit related-field querysets in the serializer to the same scope; keep
   `workspace` read-only and set it in `perform_create` from `self.workspace`.
6. Add tests for: non-member (404), forbidden role (403), hidden project (404),
   foreign ids in the payload (400/404).
