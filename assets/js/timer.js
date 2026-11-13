/* ==========================================================================
   AUCTION COUNTDOWN TIMER (assets/js/timer.js)
   ========================================================================== */

function initAuctionCountdown(endTimeString, elementId) {
  const targetTime = new Date(endTimeString).getTime();
  const el = document.getElementById(elementId);
  if (!el) return;

  const timerInterval = setInterval(() => {
    const now = new Date().getTime();
    const distance = targetTime - now;

    if (distance < 0) {
      clearInterval(timerInterval);
      el.textContent = "Auction Ended";
      el.style.color = "#dc2626";
      return;
    }

    const hours = Math.floor((distance % (1000 * 60 * 60 * 24)) / (1000 * 60 * 60));
    const minutes = Math.floor((distance % (1000 * 60 * 60)) / (1000 * 60));
    const seconds = Math.floor((distance % (1000 * 60)) / 1000);

    el.textContent = `${String(hours).padStart(2, '0')}h : ${String(minutes).padStart(2, '0')}m : ${String(seconds).padStart(2, '0')}s`;
  }, 1000);
}