/**
 * script.js — Pulsfi agency site interactions.
 * Scroll-reveal, animated stat counters, mobile nav toggle, and a
 * client-side contact form handler (posts to /api/leads/contact if an
 * API server is reachable, otherwise falls back to a mailto draft).
 */
(function () {
  'use strict';

  /* ---------------------------------------------------------- */
  /* Scroll-reveal                                               */
  /* ---------------------------------------------------------- */
  const revealEls = document.querySelectorAll('.reveal');
  const revealObserver = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          entry.target.classList.add('in-view');
          revealObserver.unobserve(entry.target);
        }
      });
    },
    { threshold: 0.15, rootMargin: '0px 0px -40px 0px' }
  );
  revealEls.forEach((el) => revealObserver.observe(el));

  /* ---------------------------------------------------------- */
  /* Animated stat counters (hero trust row)                     */
  /* ---------------------------------------------------------- */
  const counters = document.querySelectorAll('.num[data-count]');
  const countObserver = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        animateCount(entry.target);
        countObserver.unobserve(entry.target);
      });
    },
    { threshold: 0.6 }
  );
  counters.forEach((el) => countObserver.observe(el));

  function animateCount(el) {
    const target = parseInt(el.dataset.count, 10) || 0;
    const duration = 1400;
    const start = performance.now();

    function tick(now) {
      const progress = Math.min((now - start) / duration, 1);
      const eased = 1 - Math.pow(1 - progress, 3); // ease-out cubic
      el.textContent = Math.round(eased * target);
      if (progress < 1) requestAnimationFrame(tick);
    }
    requestAnimationFrame(tick);
  }

  /* ---------------------------------------------------------- */
  /* Mobile nav toggle                                            */
  /* ---------------------------------------------------------- */
  const navToggle = document.getElementById('navToggle');
  const navLinks = document.querySelector('.nav-links');
  if (navToggle && navLinks) {
    navToggle.addEventListener('click', () => {
      const isOpen = navLinks.classList.toggle('nav-links-open');
      navToggle.textContent = isOpen ? '✕' : '☰';
    });
    navLinks.querySelectorAll('a').forEach((a) =>
      a.addEventListener('click', () => {
        navLinks.classList.remove('nav-links-open');
        navToggle.textContent = '☰';
      })
    );
  }

  /* ---------------------------------------------------------- */
  /* Contact form                                                 */
  /* ---------------------------------------------------------- */
  const form = document.getElementById('contactForm');
  const note = document.getElementById('formNote');
  const API_BASE = window.PULSFI_API_BASE || 'http://127.0.0.1:5055';

  if (form) {
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const submitBtn = form.querySelector('button[type="submit"]');
      const original = submitBtn.textContent;
      submitBtn.disabled = true;
      submitBtn.textContent = 'Sending…';

      const data = Object.fromEntries(new FormData(form).entries());

      try {
        const res = await fetch(`${API_BASE}/api/contact`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(data),
        });
        if (!res.ok) throw new Error('API unavailable');
        note.textContent = "Thanks — we'll send your audit within 48 hours.";
        note.style.color = 'var(--green)';
        form.reset();
      } catch (err) {
        // Graceful fallback when no backend is running: open a pre-filled
        // email draft so the lead is never lost.
        const subject = encodeURIComponent(`Free audit request — ${data.business || 'New lead'}`);
        const body = encodeURIComponent(
          `Name: ${data.name}\nBusiness: ${data.business}\nEmail: ${data.email}\nWebsite: ${data.website || 'N/A'}\n\n${data.message || ''}`
        );
        window.location.href = `mailto:hello@pulsfi.com?subject=${subject}&body=${body}`;
        note.textContent = 'Opening your email client to send the request…';
        note.style.color = 'var(--amber)';
      } finally {
        submitBtn.disabled = false;
        submitBtn.textContent = original;
      }
    });
  }
})();
