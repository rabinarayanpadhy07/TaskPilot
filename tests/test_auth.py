import re

from django.conf import settings
from django.core import mail
from django.test import TestCase
from rest_framework.test import APIClient

from tests.factories import Team, client_for, make_user, results
from workspaces.models import Team as WorkspaceTeam
from workspaces.models import WorkspaceMembership

COOKIE = settings.REFRESH_COOKIE["name"]
PASSWORD = "s3cure-pass-123"


class AuthFlowTests(TestCase):
    def setUp(self):
        self.user = make_user("Riley Chen", "riley@example.com")
        self.client = APIClient()

    def login(self, remember=True, password=PASSWORD):
        return self.client.post("/api/auth/login/", {"email": "Riley@Example.com", "password": password, "remember": remember}, format="json")

    def test_register_validates_and_creates_account(self):
        weak = self.client.post("/api/auth/register/", {"email": "new@example.com", "full_name": "New", "password": "123"}, format="json")
        self.assertEqual(weak.status_code, 400)
        self.assertIn("password", weak.json())
        dup = self.client.post("/api/auth/register/", {"email": "RILEY@example.com", "full_name": "X", "password": "another-good-pass-9"}, format="json")
        self.assertEqual(dup.status_code, 400)
        ok = self.client.post("/api/auth/register/", {"email": "New@Example.com", "full_name": "New Person", "password": "another-good-pass-9"}, format="json")
        self.assertEqual(ok.status_code, 201)
        self.assertEqual(ok.json()["email"], "new@example.com")
        self.assertNotIn("password", ok.json())

    def test_login_returns_access_and_sets_httponly_refresh_cookie(self):
        response = self.login()
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("access", body)
        self.assertNotIn("refresh", body)  # never exposed to JavaScript
        self.assertEqual(body["user"]["email"], "riley@example.com")
        cookie = response.cookies[COOKIE]
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["path"], "/api/auth/")
        self.assertEqual(cookie["samesite"], "Lax")
        self.assertTrue(cookie["max-age"])

    def test_session_cookie_when_not_remembered(self):
        cookie = self.login(remember=False).cookies[COOKIE]
        self.assertEqual(cookie["max-age"], "")
        refreshed = self.client.post("/api/auth/token/refresh/")
        self.assertEqual(refreshed.cookies[COOKIE]["max-age"], "")  # stays a session cookie after rotation

    def test_wrong_password_is_a_generic_401(self):
        response = self.login(password="nope")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "Incorrect email or password.")
        self.assertNotIn(COOKIE, response.cookies)

    def test_access_token_authenticates_api_calls(self):
        access = self.login().json()["access"]
        self.assertEqual(APIClient().get("/api/auth/me/").status_code, 401)
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        self.assertEqual(api.get("/api/auth/me/").json()["email"], "riley@example.com")

    def test_refresh_rotates_and_old_token_is_rejected(self):
        self.login()
        old = self.client.cookies[COOKIE].value
        first = self.client.post("/api/auth/token/refresh/")
        self.assertEqual(first.status_code, 200)
        self.assertIn("access", first.json())
        new = first.cookies[COOKIE].value
        self.assertNotEqual(old, new)
        # A just-rotated token is honoured once more (lost Set-Cookie, parallel tabs)...
        replay = APIClient()
        replay.cookies[COOKIE] = old
        grace = replay.post("/api/auth/token/refresh/")
        self.assertEqual(grace.status_code, 200)
        self.assertNotIn(grace.cookies[COOKIE].value, (old, new))
        # ...and again within the window (repeated interrupted reloads)
        again = APIClient()
        again.cookies[COOKIE] = old
        self.assertEqual(again.post("/api/auth/token/refresh/").status_code, 200)

    def test_no_grace_after_rotation_window_or_logout(self):
        from django.core.cache import cache

        self.login()
        old = self.client.cookies[COOKIE].value
        self.client.post("/api/auth/token/refresh/")
        cache.clear()  # window expired
        replay = APIClient()
        replay.cookies[COOKIE] = old
        self.assertEqual(replay.post("/api/auth/token/refresh/").status_code, 401)
        # Refresh then log out: the rotated (previous) token gets no grace either
        self.login()
        rotated_away = self.client.cookies[COOKIE].value
        self.client.post("/api/auth/token/refresh/")
        self.client.post("/api/auth/logout/")
        replay = APIClient()
        replay.cookies[COOKIE] = rotated_away
        self.assertEqual(replay.post("/api/auth/token/refresh/").status_code, 401)
        # Logging out revokes the current token with no grace
        self.login()
        current = self.client.cookies[COOKIE].value
        self.client.post("/api/auth/logout/")
        replay = APIClient()
        replay.cookies[COOKIE] = current
        self.assertEqual(replay.post("/api/auth/token/refresh/").status_code, 401)

    def test_refresh_without_cookie_is_401(self):
        self.assertEqual(self.client.post("/api/auth/token/refresh/").status_code, 401)

    def test_refresh_rejects_foreign_origin(self):
        self.login()
        response = self.client.post("/api/auth/token/refresh/", HTTP_ORIGIN="https://evil.example")
        self.assertEqual(response.status_code, 403)
        allowed = self.client.post("/api/auth/token/refresh/", HTTP_ORIGIN=settings.CORS_ALLOWED_ORIGINS[0])
        self.assertEqual(allowed.status_code, 200)

    def test_logout_blacklists_refresh_token(self):
        self.login()
        token = self.client.cookies[COOKIE].value
        response = self.client.post("/api/auth/logout/")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.cookies[COOKIE].value, "")
        replay = APIClient()
        replay.cookies[COOKIE] = token
        self.assertEqual(replay.post("/api/auth/token/refresh/").status_code, 401)

    def test_profile_update_cannot_change_email(self):
        api = client_for(self.user)
        response = api.patch("/api/auth/me/", {"full_name": "Riley C.", "title": "PM", "email": "x@evil.example"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual((self.user.full_name, self.user.title, self.user.email), ("Riley C.", "PM", "riley@example.com"))

    def test_password_change_revokes_other_sessions(self):
        self.login()
        other_session = self.client.cookies[COOKIE].value
        api = client_for(self.user)
        wrong = api.post("/api/auth/password/change/", {"current_password": "nope", "new_password": "brand-new-pass-77"}, format="json")
        self.assertEqual(wrong.status_code, 400)
        ok = api.post("/api/auth/password/change/", {"current_password": PASSWORD, "new_password": "brand-new-pass-77"}, format="json")
        self.assertEqual(ok.status_code, 200)
        self.assertIn(COOKIE, ok.cookies)
        replay = APIClient()
        replay.cookies[COOKIE] = other_session
        self.assertEqual(replay.post("/api/auth/token/refresh/").status_code, 401)

    def test_password_reset_flow(self):
        unknown = self.client.post("/api/auth/password-reset/", {"email": "nobody@example.com"}, format="json")
        self.assertEqual(unknown.status_code, 204)
        self.assertEqual(len(mail.outbox), 0)
        known = self.client.post("/api/auth/password-reset/", {"email": "riley@example.com"}, format="json")
        self.assertEqual(known.status_code, 204)  # identical response
        self.assertEqual(len(mail.outbox), 1)
        uid, token = re.search(r"/reset-password/([^/]+)/([^/\s]+)", mail.outbox[0].body).groups()

        bad = self.client.post("/api/auth/password-reset/confirm/", {"uid": uid, "token": "bad-token", "password": "reset-pass-55x"}, format="json")
        self.assertEqual(bad.status_code, 400)
        weak = self.client.post("/api/auth/password-reset/confirm/", {"uid": uid, "token": token, "password": "123"}, format="json")
        self.assertEqual(weak.status_code, 400)
        ok = self.client.post("/api/auth/password-reset/confirm/", {"uid": uid, "token": token, "password": "reset-pass-55x"}, format="json")
        self.assertEqual(ok.status_code, 204)
        self.assertEqual(self.login(password="reset-pass-55x").status_code, 200)
        reused = self.client.post("/api/auth/password-reset/confirm/", {"uid": uid, "token": token, "password": "another-pass-66y"}, format="json")
        self.assertEqual(reused.status_code, 400)  # tokens are single-use

    def test_inactive_user_cannot_log_in(self):
        self.user.is_active = False
        self.user.save()
        self.assertEqual(self.login().status_code, 401)


class WorkspaceManagementTests(TestCase):
    def setUp(self):
        self.team = Team()

    def test_create_workspace_makes_creator_owner(self):
        user = make_user()
        response = client_for(user).post("/api/workspaces/", {"name": "New Co", "slug": "new-co"}, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["role"], "owner")
        self.assertEqual(WorkspaceMembership.objects.get(user=user).role, "owner")
        taken = client_for(make_user()).post("/api/workspaces/", {"name": "Dup", "slug": "new-co"}, format="json")
        self.assertEqual(taken.status_code, 400)
        bad = client_for(make_user()).post("/api/workspaces/", {"name": "Bad", "slug": "Bad Slug!"}, format="json")
        self.assertEqual(bad.status_code, 400)

    def test_workspace_update_needs_workspace_update_permission(self):
        t = self.team
        url = f"/api/workspaces/{t.ws.pk}/"
        self.assertEqual(client_for(t.dev).patch(url, {"name": "Hacked"}, format="json").status_code, 403)
        response = client_for(t.admin).patch(url, {"name": "Acme Corp"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["name"], "Acme Corp")
        self.assertEqual(client_for(t.outsider).patch(url, {"name": "X"}, format="json").status_code, 404)

    def test_only_owner_deletes_workspace(self):
        t = self.team
        url = f"/api/workspaces/{t.ws.pk}/"
        self.assertEqual(client_for(t.admin).delete(url).status_code, 403)
        self.assertEqual(client_for(t.owner).delete(url).status_code, 204)

    def test_teams(self):
        t = self.team
        url = t.url("teams/")
        payload = {"name": "Platform", "member_ids": [t.dev.pk, t.viewer.pk]}
        self.assertEqual(client_for(t.dev).post(url, payload, format="json").status_code, 403)
        created = client_for(t.admin).post(url, payload, format="json")
        self.assertEqual(created.status_code, 201)
        self.assertEqual(sorted(created.json()["member_ids"]), sorted([t.dev.pk, t.viewer.pk]))
        foreign = client_for(t.admin).post(url, {"name": "Bad", "member_ids": [t.outsider.pk]}, format="json")
        self.assertEqual(foreign.status_code, 400)
        self.assertEqual([team["name"] for team in results(client_for(t.viewer).get(url))], ["Platform"])
        # Removing a member takes them out of the workspace's teams
        client_for(t.admin).delete(t.url(f"members/{t.dev.pk}/"))
        self.assertEqual(list(WorkspaceTeam.objects.get(name="Platform").members.values_list("pk", flat=True)), [t.viewer.pk])


class ThrottleTests(TestCase):
    def setUp(self):
        from django.core.cache import cache

        cache.clear()
        make_user("Riley Chen", "riley@example.com")

    def test_login_is_throttled_but_refresh_has_its_own_budget(self):
        from unittest import mock

        from rest_framework.throttling import ScopedRateThrottle

        rates = {"auth": "3/min", "refresh": "120/min", "invitation": "30/min", "password_reset": "5/hour"}
        with mock.patch.object(ScopedRateThrottle, "THROTTLE_RATES", rates):
            client = APIClient()
            body = {"email": "riley@example.com", "password": PASSWORD}
            self.assertEqual(client.post("/api/auth/login/", body, format="json").status_code, 200)
            # Page loads refresh often; that must not eat the login budget or get throttled
            for _ in range(10):
                self.assertEqual(client.post("/api/auth/token/refresh/").status_code, 200)
            self.assertEqual(client.post("/api/auth/login/", body, format="json").status_code, 200)
            self.assertEqual(client.post("/api/auth/login/", body, format="json").status_code, 200)
            self.assertEqual(client.post("/api/auth/login/", body, format="json").status_code, 429)
