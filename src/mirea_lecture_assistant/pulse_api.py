"""Compatibility with Pulse's renamed authentication cookie."""
from pymirea.grades import MireaGrades as UpstreamGrades

PULSE_COOKIE_NAMES = ("Pulse.Auth.Cookie", ".AspNetCore.Cookies")


class MireaGrades(UpstreamGrades):
    async def _ensure_aspnet_cookie(self):
        # The actual gRPC response remains the authentication verdict; the
        # presence of either cookie only bypasses upstream's obsolete bootstrap.
        current = self.client.cookies.get("Pulse.Auth.Cookie")
        if current:
            self.session_cookies["Pulse.Auth.Cookie"] = current
            return True, None
        return await super()._ensure_aspnet_cookie()
