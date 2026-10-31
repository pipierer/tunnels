/* dashboard.js — refresh tunnel status and wire up the copy buttons. */
(function () {
  const statusBadge = document.getElementById('status-badge');
  const statusLabel = statusBadge.querySelector('.label');
  const urlEl = document.getElementById('public-url');
  const portEl = document.getElementById('local-port');
  const lastSeenEl = document.getElementById('last-seen');
  const wsUrlEl = document.getElementById('ws-url');

  function setStatus(state, label) {
    statusBadge.dataset.status = state;
    statusLabel.textContent = label;
  }

  function fmtLastSeen(iso) {
    if (!iso) return 'jamais';
    try {
      const d = new Date(iso);
      return d.toLocaleString('fr-FR');
    } catch (_) {
      return iso;
    }
  }

  async function refresh() {
    try {
      const { ok, data } = await window.BT.api('/api/tunnels/me');
      if (!ok || !data) return;
      urlEl.textContent = data.public_url || '—';
      portEl.textContent = data.local_port != null ? data.local_port : '—';
      lastSeenEl.textContent = fmtLastSeen(data.last_seen_at);
      if (data.is_active) {
        setStatus('online', 'En ligne');
      } else {
        setStatus('offline', 'Hors ligne');
      }
    } catch (err) {
      setStatus('error', 'Erreur');
    }
  }

  function copy(text, btn) {
    navigator.clipboard.writeText(text).then(() => {
      const original = btn.textContent;
      btn.textContent = 'Copié !';
      setTimeout(() => { btn.textContent = original; }, 1500);
    });
  }

  document.getElementById('copy-url')?.addEventListener('click', (e) => {
    copy(urlEl.textContent, e.currentTarget);
  });

  document.getElementById('copy-start')?.addEventListener('click', (e) => {
    const txt = document.getElementById('start-cmd').innerText;
    copy(txt, e.currentTarget);
  });

  document.getElementById('btn-start')?.addEventListener('click', async () => {
    // Fetch a fresh WS token (informational only — the CLI fetches its own).
    try {
      const { ok, data } = await window.BT.api('/api/tunnels/token', { method: 'POST' });
      if (ok && data && data.token) {
        const hint = document.getElementById('tunnel-token-hint');
        const code = document.getElementById('tunnel-token');
        if (hint && code) {
          code.textContent = data.token.slice(0, 16) + '…';
          hint.hidden = false;
        }
      }
    } catch (_) { /* ignore */ }
    refresh();
  });

  document.getElementById('btn-stop')?.addEventListener('click', () => {
    alert('Pour stopper le tunnel : Ctrl+C dans le terminal où tourne le client Python.');
  });

  // Poll every 5s.
  refresh();
  setInterval(refresh, 5000);
})();
