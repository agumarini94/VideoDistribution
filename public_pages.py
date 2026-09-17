# public_pages.py
# Páginas públicas de Arscor: landing (/), términos (/terms) y privacidad (/privacy).
#
# CÓMO SE USA (2 líneas en tu main.py, donde creás la app de FastAPI):
#
#     from public_pages import public_router
#     app.include_router(public_router)
#
# OJO: si tu app ya tiene una ruta "/" (por ejemplo el operator UI),
# movela a "/dashboard" o similar, así la landing queda en la raíz.
# Los botones "Log in" / "Sign up" apuntan a /login y /signup:
# cambialos abajo (BASE_HEAD -> nav) si tus rutas se llaman distinto.

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

public_router = APIRouter()

# ---------------------------------------------------------------- estilos ---

BASE_HEAD = """
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@500;700&family=Space+Grotesk:wght@400;500;700&display=swap" rel="stylesheet">
<style>
  :root{
    --violet:#6D28D9;
    --violet-soft:#EDE4FF;
    --ink:#141018;
    --paper:#FBF9FF;
    --lime:#D8F34E;
    --shadow:5px 5px 0 var(--ink);
  }
  *{box-sizing:border-box;margin:0;padding:0}
  body{
    font-family:"Space Grotesk",sans-serif;
    background:var(--paper);
    color:var(--ink);
    line-height:1.55;
  }
  a{color:inherit}
  .wrap{max-width:960px;margin:0 auto;padding:0 20px}
  /* ------- nav ------- */
  header{border-bottom:3px solid var(--ink);background:var(--paper)}
  .nav{display:flex;align-items:center;justify-content:space-between;padding:18px 0}
  .logo{font-family:"IBM Plex Mono",monospace;font-weight:700;font-size:1.35rem;
        text-decoration:none;letter-spacing:-0.5px}
  .logo span{color:var(--violet)}
  .nav-actions{display:flex;gap:12px}
  .btn{display:inline-block;font-family:"IBM Plex Mono",monospace;font-weight:700;
       font-size:0.95rem;text-decoration:none;padding:10px 20px;
       border:3px solid var(--ink);background:#fff;color:var(--ink);
       box-shadow:var(--shadow);transition:transform .08s ease,box-shadow .08s ease}
  .btn:hover{transform:translate(2px,2px);box-shadow:3px 3px 0 var(--ink)}
  .btn:focus-visible{outline:3px solid var(--violet);outline-offset:3px}
  .btn-primary{background:var(--violet);color:#fff}
  /* ------- footer ------- */
  footer{border-top:3px solid var(--ink);margin-top:80px}
  .foot{display:flex;flex-wrap:wrap;gap:16px;justify-content:space-between;
        padding:26px 0;font-size:0.95rem}
  .foot nav{display:flex;gap:22px}
  /* ------- páginas legales ------- */
  .legal{padding:56px 0;max-width:760px}
  .legal h1{font-family:"IBM Plex Mono",monospace;font-size:2rem;margin-bottom:6px}
  .legal .updated{color:#5b5266;margin-bottom:36px}
  .legal h2{font-family:"IBM Plex Mono",monospace;font-size:1.15rem;
            margin:34px 0 10px;border-bottom:3px solid var(--violet-soft);
            padding-bottom:6px}
  .legal p, .legal li{margin-bottom:12px}
  .legal ul{padding-left:22px}
  @media (prefers-reduced-motion:reduce){ .btn{transition:none} }
</style>
"""

def _page(title: str, body: str) -> str:
    # "Log in" / "Sign up" both point at the operator/client SPA mounted at
    # /dashboard (dashboard/api.py, dashboard/static/index.html) — there are
    # no separate backend /login or /signup pages. The SPA already handles
    # both: GET /dashboard checks GET /api/auth/me and, for an anonymous
    # visitor, renders its own Login/Register screens (Phase 28) instead of
    # the app. "Sign up" adds ?auth=signup, a query hint boot() reads once
    # to pre-select the Register form (the SPA otherwise defaults to Login,
    # requiring an extra "Request access" click) — same query-param-driven
    # boot pattern already used for the OAuth-connect notices
    # (consumeOauthRedirectParams).
    return f"""<!doctype html>
<html lang="en">
<head><title>{title}</title>{BASE_HEAD}</head>
<body>
<header>
  <div class="wrap nav">
    <a class="logo" href="/">arscor<span>.</span></a>
    <div class="nav-actions">
      <a class="btn" href="/dashboard">Log in</a>
      <a class="btn btn-primary" href="/dashboard?auth=signup">Sign up</a>
    </div>
  </div>
</header>
{body}
<footer>
  <div class="wrap foot">
    <div>© 2026 Arscor — tech@arscor.io</div>
    <nav>
      <a href="/terms">Terms of Service</a>
      <a href="/privacy">Privacy Policy</a>
    </nav>
  </div>
</footer>
</body>
</html>"""

# ---------------------------------------------------------------- landing ---

LANDING_BODY = """
<style>
  .hero{padding:72px 0 40px}
  .hero h1{font-family:"IBM Plex Mono",monospace;font-weight:700;
           font-size:clamp(2rem,5.5vw,3.4rem);line-height:1.12;
           letter-spacing:-1px;max-width:17ch}
  .hero h1 em{font-style:normal;background:var(--lime);
              border:3px solid var(--ink);padding:0 10px;display:inline-block;
              box-shadow:var(--shadow)}
  .hero p{font-size:1.2rem;max-width:52ch;margin:26px 0 34px}
  .platforms{font-family:"IBM Plex Mono",monospace;font-size:0.95rem;
             color:#5b5266;margin-top:18px}
  .how{padding:48px 0}
  .how h2{font-family:"IBM Plex Mono",monospace;font-size:1.5rem;margin-bottom:26px}
  .steps{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:22px}
  .step{border:3px solid var(--ink);background:#fff;box-shadow:var(--shadow);padding:22px}
  .step .n{font-family:"IBM Plex Mono",monospace;font-weight:700;
           display:inline-block;background:var(--violet);color:#fff;
           border:3px solid var(--ink);padding:2px 12px;margin-bottom:14px}
  .step h3{font-size:1.05rem;margin-bottom:8px}
  .feat{padding:40px 0}
  .feat ul{list-style:none;display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px}
  .feat li{border:3px solid var(--ink);background:var(--violet-soft);padding:14px 18px}
  .cta{padding:56px 0;text-align:left}
  .cta .btn{font-size:1.1rem;padding:14px 28px}
</style>

<main class="wrap">
  <section class="hero">
    <h1>Publish your videos <em>everywhere</em>, on schedule.</h1>
    <p>Arscor is a web platform where creators and businesses connect their own
       social accounts, upload videos once, and let Arscor deliver them to each
       platform at the time they choose.</p>
    <a class="btn btn-primary" href="/dashboard?auth=signup">Create your account</a>
    <p class="platforms">Works with YouTube · TikTok · X</p>
  </section>

  <section class="how">
    <h2>How it works</h2>
    <div class="steps">
      <div class="step"><span class="n">1</span>
        <h3>Connect your accounts</h3>
        <p>Sign in with each platform in one click. Arscor never sees your
           passwords — you authorize access directly on each network.</p>
      </div>
      <div class="step"><span class="n">2</span>
        <h3>Upload and schedule</h3>
        <p>Add a video, pick the platform and the account, set a date and time.
           Queue as many posts as you need.</p>
      </div>
      <div class="step"><span class="n">3</span>
        <h3>Arscor publishes for you</h3>
        <p>Your videos go out on schedule. If a platform hiccups, Arscor
           retries automatically and shows you the status of every post.</p>
      </div>
    </div>
  </section>

  <section class="feat">
    <ul>
      <li>Live dashboard with the status of every scheduled post</li>
      <li>Automatic retries when a platform fails</li>
      <li>Multiple accounts per platform</li>
      <li>Your credentials stay private and are refreshed securely</li>
    </ul>
  </section>

  <section class="cta">
    <a class="btn btn-primary" href="/dashboard?auth=signup">Start publishing</a>
  </section>
</main>
"""

# ------------------------------------------------------------------ terms ---

TERMS_BODY = """
<main class="wrap legal">
  <h1>Terms of Service</h1>
  <p class="updated">Last updated: September 15, 2026</p>

  <p>These Terms of Service ("Terms") govern your use of Arscor, a web platform
     operated by Arscor ("we", "us"), available at app.arscor.io. By creating an
     account or using the service, you agree to these Terms.</p>

  <h2>1. What Arscor does</h2>
  <p>Arscor lets you connect your own social media accounts (such as YouTube,
     TikTok and X) and schedule and publish your video content to those accounts.</p>

  <h2>2. Your account</h2>
  <ul>
    <li>You must provide accurate information when signing up and keep your
        login credentials secure.</li>
    <li>You are responsible for all activity that happens under your account.</li>
    <li>You must be legally able to enter into this agreement in your country.</li>
  </ul>

  <h2>3. Your content</h2>
  <ul>
    <li>You keep full ownership of the videos and content you upload.</li>
    <li>You grant us only the limited permission needed to store your content and
        deliver it to the platforms you choose, on your behalf.</li>
    <li>You confirm you have the rights to publish everything you upload.</li>
  </ul>

  <h2>4. Acceptable use</h2>
  <p>You agree not to use Arscor to:</p>
  <ul>
    <li>Publish content you don't have the rights to, or content that is illegal,
        or that violates the rules of the destination platform.</li>
    <li>Send spam, run coordinated inauthentic account networks, or artificially
        manipulate engagement.</li>
    <li>Interfere with the security or operation of the service.</li>
  </ul>
  <p>When you connect a third-party account, you must also comply with that
     platform's own terms (for example, TikTok's Terms of Service and Community
     Guidelines). We may suspend or terminate accounts that violate this section.</p>

  <h2>5. Third-party platforms</h2>
  <p>Arscor connects to third-party platforms through their official tools. Those
     platforms may change or limit their services at any time; we are not
     responsible for their availability or decisions (for example, a platform
     rejecting or removing a post).</p>

  <h2>6. Disconnecting and termination</h2>
  <p>You can disconnect any linked social account at any time from your dashboard
     or from the platform's own security settings, and you can delete your Arscor
     account by contacting us. We may suspend or close accounts that break these
     Terms.</p>

  <h2>7. Service "as is"</h2>
  <p>Arscor is provided "as is" and "as available", without warranties of any
     kind. To the maximum extent permitted by law, we are not liable for indirect
     or consequential damages arising from the use of the service.</p>

  <h2>8. Changes to these Terms</h2>
  <p>We may update these Terms from time to time. If we make material changes, we
     will notify you through the service or by email. Continuing to use Arscor
     after changes take effect means you accept the new Terms.</p>

  <h2>9. Contact</h2>
  <p>Questions about these Terms: <a href="mailto:tech@arscor.io">tech@arscor.io</a>.</p>
</main>
"""

# ---------------------------------------------------------------- privacy ---

PRIVACY_BODY = """
<main class="wrap legal">
  <h1>Privacy Policy</h1>
  <p class="updated">Last updated: September 15, 2026</p>

  <p>This Privacy Policy explains what information Arscor ("we", "us") collects
     when you use app.arscor.io, how we use it, and the choices you have.</p>

  <h2>1. Information we collect</h2>
  <ul>
    <li><strong>Account information:</strong> your email address and password
        (stored hashed) when you sign up.</li>
    <li><strong>Connected accounts:</strong> when you link a social media account
        (such as TikTok, YouTube or X), we receive basic profile information
        (such as your display name and avatar) and access tokens that let us
        publish on your behalf. We never receive or store your social media
        passwords.</li>
    <li><strong>Your content:</strong> the videos, titles and schedules you upload
        so we can publish them to the platforms you choose.</li>
    <li><strong>Usage and technical data:</strong> basic logs (such as IP address,
        browser type and timestamps) used for security and to keep the service
        working.</li>
  </ul>

  <h2>2. How we use your information</h2>
  <ul>
    <li>To operate the service: store your videos and publish them to the
        accounts and schedules you set.</li>
    <li>To show you the status of your posts in your dashboard.</li>
    <li>To secure the service and prevent abuse.</li>
    <li>To contact you about your account when needed.</li>
  </ul>
  <p>We do not sell your personal information, and we do not share it with third
     parties except the platforms you explicitly connect (to publish your
     content) and the infrastructure providers that host the service.</p>

  <h2>3. How we store and protect it</h2>
  <p>Data is stored on secure cloud infrastructure. Access tokens for connected
     accounts are stored encrypted, refreshed automatically, and are never shown
     in the interface. Access to production data is restricted.</p>

  <h2>4. Data retention and deletion</h2>
  <ul>
    <li>Uploaded videos are kept only as long as needed to publish them and show
        you their status.</li>
    <li>You can disconnect a linked social account at any time from your
        dashboard or from that platform's security settings; we then delete the
        associated tokens.</li>
    <li>You can request deletion of your entire account and data by writing to
        <a href="mailto:tech@arscor.io">tech@arscor.io</a>. We will delete it
        within 30 days.</li>
  </ul>

  <h2>5. Third-party platforms</h2>
  <p>When you connect and publish to a platform such as TikTok, YouTube or X,
     that platform processes your data under its own privacy policy. Please
     review the privacy policy of each platform you connect.</p>

  <h2>6. Children</h2>
  <p>Arscor is not directed at children and may not be used by anyone under the
     minimum age required by the platforms it connects to.</p>

  <h2>7. Changes to this policy</h2>
  <p>We may update this policy from time to time. If we make material changes,
     we will notify you through the service or by email.</p>

  <h2>8. Contact</h2>
  <p>Privacy questions or requests:
     <a href="mailto:tech@arscor.io">tech@arscor.io</a>.</p>
</main>
"""

# ------------------------------------------------------------------ rutas ---

@public_router.get("/", response_class=HTMLResponse, include_in_schema=False)
def landing() -> str:
    return _page("Arscor — Schedule and publish your videos", LANDING_BODY)

@public_router.get("/terms", response_class=HTMLResponse, include_in_schema=False)
def terms() -> str:
    return _page("Terms of Service — Arscor", TERMS_BODY)

@public_router.get("/privacy", response_class=HTMLResponse, include_in_schema=False)
def privacy() -> str:
    return _page("Privacy Policy — Arscor", PRIVACY_BODY)