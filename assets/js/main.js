/* ==========================================================================
   MAIN APP HELPERS (assets/js/main.js)
   ========================================================================== */

function getLoggedInUser() {
  return JSON.parse(localStorage.getItem('farmbid_user') || localStorage.getItem('user') || localStorage.getItem('currentUser'));
}

document.addEventListener('DOMContentLoaded', () => {
  const user = getLoggedInUser();
  const roleBadge = document.querySelector('.badge-role, .user-role-indicator');
  if (roleBadge && user) {
    roleBadge.textContent = user.role ? user.role.toUpperCase() : 'USER';
  }
});