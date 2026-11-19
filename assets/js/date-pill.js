(function () {
  function formatPHTDate(dateObj = new Date()) {
    return new Intl.DateTimeFormat('en-US', {
      month: 'short',
      day: 'numeric',
      year: 'numeric',
      timeZone: 'Asia/Manila'
    }).format(dateObj);
  }

  function updateDatePills() {
    const formatted = formatPHTDate(new Date());

    const selectors = [
      '#currentDatePill',
      '#headerDatePill',
      '.date-pill',
      '[data-dynamic-date]'
    ];

    const matched = new Set();
    selectors.forEach((sel) => {
      document.querySelectorAll(sel).forEach((el) => matched.add(el));
    });

    // Match any leaf element holding a static "Mon DD, YYYY" date anywhere on the page
    document.querySelectorAll('span, div, button, p, time').forEach((el) => {
      if (
        el.children.length === 0 &&
        /^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},\s+\d{4}$/i.test(el.textContent.trim())
      ) {
        matched.add(el);
      }
    });

    matched.forEach((el) => {
      el.textContent = formatted;
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', updateDatePills);
  } else {
    updateDatePills();
  }
})();