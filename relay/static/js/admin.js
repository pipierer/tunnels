/* admin.js — tabs, table rendering, action buttons, modal confirmation. */
(function () {
  const tabs = document.querySelectorAll('.tab');
  const panels = document.querySelectorAll('.tab-panel');
  tabs.forEach((tab) => {
    tab.addEventListener('click', () => {
      tabs.forEach((t) => t.classList.remove('active'));
      panels.forEach((p) => p.classList.remove('active'));
      tab.classList.add('active');
      document.getElementById('tab-' + tab.dataset.tab).classList.add('active');
    });
  });

  // ----- Modal -----------------------------------------------------------
  const modal = document.getElementById('modal');
  const modalTitle = document.getElementById('modal-title');
  const modalBody = document.getElementById('modal-body');
  const modalResult = document.getElementById('modal-result');
  const btnCancel = document.getElementById('modal-cancel');
  const btnConfirm = document.getElementById('modal-confirm');

  function closeModal() {
    modal.hidden = true;
    modalTitle.textContent = 'Confirmation';
    modalBody.textContent = '';
    modalResult.hidden = true;
    modalResult.textContent = '';
    btnConfirm.onclick = null;
  }
  btnCancel.addEventListener('click', closeModal);
  modal.addEventListener('click', (e) => { if (e.target === modal) closeModal(); });

  function confirmAction(title, body, onConfirm) {
    modalTitle.textContent = title;
    modalBody.textContent = body;
    modalResult.hidden = true;
    modalResult.textContent = '';
    btnConfirm.onclick = async () => {
      btnConfirm.disabled = true;
      try {
        const result = await onConfirm();
        if (result) {
          modalResult.textContent = result;
          modalResult.hidden = false;
          // Keep modal open so admin can copy the new password etc.
        } else {
          closeModal();
        }
      } catch (err) {
        modalResult.textContent = 'Erreur : ' + (err.message || err);
        modalResult.hidden = false;
      } finally {
        btnConfirm.disabled = false;
      }
    };
    modal.hidden = false;
  }

  // ----- Helpers ---------------------------------------------------------
  function fmt(iso) {
    if (!iso) return '—';
    try { return new Date(iso).toLocaleString('fr-FR'); } catch (_) { return iso; }
  }
  function statusPill(active) {
    return active
      ? '<span class="pill online">en ligne</span>'
      : '<span class="pill offline">hors ligne</span>';
  }
  function escapeHtml(s) {
    if (s == null) return '';
    return String(s).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
  }

  // ----- Users tab -------------------------------------------------------
  async function loadUsers() {
    try {
      const { data } = await window.BT.api('/api/admin/users');
      const tbody = document.querySelector('#users-table tbody');
      tbody.innerHTML = '';
      (data.users || []).forEach((u) => {
        const tr = document.createElement('tr');
        const tunnel = u.tunnel || {};
        const status = u.is_suspended
          ? '<span class="pill suspended">suspendu</span>'
          : statusPill(tunnel.is_active);
        const role = u.is_admin
          ? '<span class="pill admin">admin</span>'
          : '<span class="pill">user</span>';
        const suspendBtn = u.is_suspended
          ? `<button data-action="unsuspend" data-id="${u.id}">Réactiver</button>`
          : `<button data-action="suspend" data-id="${u.id}">Suspendre</button>`;
        tr.innerHTML = `
          <td>${u.id}</td>
          <td>${escapeHtml(u.email)}</td>
          <td>${escapeHtml(u.first_name)}</td>
          <td>${role}</td>
          <td>${status}</td>
          <td>${escapeHtml(tunnel.tunnel_id || '—')}</td>
          <td>${fmt(tunnel.last_seen_at)}</td>
          <td class="row-actions">
            ${suspendBtn}
            <button data-action="reset-password" data-id="${u.id}">Reset MDP</button>
          </td>`;
        tbody.appendChild(tr);
      });
    } catch (err) {
      console.error('loadUsers', err);
    }
  }

  // ----- Tunnels tab -----------------------------------------------------
  async function loadTunnels() {
    try {
      const { data } = await window.BT.api('/api/admin/tunnels');
      const tbody = document.querySelector('#tunnels-table tbody');
      tbody.innerHTML = '';
      (data.tunnels || []).forEach((t) => {
        const tr = document.createElement('tr');
        tr.innerHTML = `
          <td><code>${escapeHtml(t.tunnel_id)}</code></td>
          <td>${escapeHtml(t.user_email)}</td>
          <td>${t.local_port != null ? t.local_port : '—'}</td>
          <td>${statusPill(t.is_active)}</td>
          <td>${fmt(t.last_seen_at)}</td>
          <td class="row-actions">
            <button data-action="revoke" data-tunnel="${escapeHtml(t.tunnel_id)}">Révoquer token</button>
            <button class="danger" data-action="delete" data-tunnel="${escapeHtml(t.tunnel_id)}">Supprimer</button>
          </td>`;
        tbody.appendChild(tr);
      });
    } catch (err) {
      console.error('loadTunnels', err);
    }
  }

  // ----- Audit tab -------------------------------------------------------
  async function loadAudit() {
    try {
      const { data } = await window.BT.api('/api/admin/audit-logs?limit=200');
      const tbody = document.querySelector('#audit-table tbody');
      tbody.innerHTML = '';
      (data.logs || []).forEach((l) => {
        const tr = document.createElement('tr');
        tr.innerHTML = `
          <td>${l.id}</td>
          <td>${fmt(l.created_at)}</td>
          <td>${l.user_id ?? '—'}</td>
          <td><code>${escapeHtml(l.action)}</code></td>
          <td>${escapeHtml(l.ip_address || '—')}</td>
          <td>${escapeHtml(l.details || '')}</td>`;
        tbody.appendChild(tr);
      });
    } catch (err) {
      console.error('loadAudit', err);
    }
  }

  // ----- Action delegation ----------------------------------------------
  document.addEventListener('click', async (e) => {
    const btn = e.target.closest('button[data-action]');
    if (!btn) return;
    const action = btn.dataset.action;
    const userId = btn.dataset.id;
    const tunnelId = btn.dataset.tunnel;

    if (action === 'suspend') {
      confirmAction('Suspendre cet utilisateur', 'Le compte sera suspendu et son tunnel déconnecté.', async () => {
        await window.BT.api(`/api/admin/users/${userId}/suspend`, { method: 'POST' });
        await loadUsers(); await loadTunnels();
      });
    } else if (action === 'unsuspend') {
      confirmAction('Réactiver cet utilisateur', 'Le compte sera réactivé.', async () => {
        await window.BT.api(`/api/admin/users/${userId}/unsuspend`, { method: 'POST' });
        await loadUsers();
      });
    } else if (action === 'reset-password') {
      confirmAction('Réinitialiser le mot de passe', 'Un nouveau mot de passe aléatoire sera généré. L\'ancien ne fonctionnera plus.', async () => {
        const { data } = await window.BT.api(`/api/admin/users/${userId}/reset-password`, { method: 'POST' });
        await loadUsers();
        return 'Nouveau mot de passe (une seule fois) : ' + data.new_password;
      });
    } else if (action === 'revoke') {
      confirmAction('Révoquer les tokens', 'Tous les JWT tunnels actifs de cet utilisateur cesseront de fonctionner.', async () => {
        await window.BT.api(`/api/admin/tunnels/${encodeURIComponent(tunnelId)}/revoke-token`, { method: 'POST' });
        await loadTunnels();
      });
    } else if (action === 'delete') {
      confirmAction('Supprimer le tunnel', 'Le tunnel sera supprimé définitivement et la WS déconnectée.', async () => {
        await window.BT.api(`/api/admin/tunnels/${encodeURIComponent(tunnelId)}`, { method: 'DELETE' });
        await loadUsers(); await loadTunnels();
      });
    }
  });

  // Initial load.
  loadUsers(); loadTunnels(); loadAudit();
  // Refresh connected-state periodically.
  setInterval(() => { loadUsers(); loadTunnels(); }, 10000);
})();
