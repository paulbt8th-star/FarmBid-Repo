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

    // Target elements by common IDs, classes, or data attributes
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

    // Fallback: match pill-like elements in page headers that currently hold a static date string
    document.querySelectorAll('.page-header-row span, .page-header-row div, .dash-topbar span, .dash-topbar div').forEach((el) => {
      if (el.children.length === 0 && /^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},\s+\d{4}$/i.test(el.textContent.trim())) {
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