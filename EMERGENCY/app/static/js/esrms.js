// Live Philippine Time (PHT, UTC+8) clock in the telemetry header.
(function () {
  const clock = document.getElementById('live-clock');
  if (!clock) return;
  const fmt = new Intl.DateTimeFormat('en-PH', {
    timeZone: 'Asia/Manila',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: true,
  });
  function tick() {
    clock.textContent = `PHT ${fmt.format(new Date())}`;
  }
  tick();
  setInterval(tick, 1000);
})();

// Show/hide password for any button with data-toggle-password="<input id>".
document.querySelectorAll('[data-toggle-password]').forEach((btn) => {
  btn.addEventListener('click', () => {
    const input = document.getElementById(btn.dataset.togglePassword);
    if (!input) return;
    const show = input.type === 'password';
    input.type = show ? 'text' : 'password';
    btn.querySelectorAll('[data-eye]').forEach((el) => {
      el.classList.toggle('hidden', el.dataset.eye !== (show ? 'closed' : 'open'));
    });
  });
});

// Loading state on submit buttons marked with data-loading-text.
document.querySelectorAll('form').forEach((form) => {
  form.addEventListener('submit', (e) => {
    const btn = e.submitter;
    if (!btn || !btn.dataset.loadingText) return;
    const label = btn.querySelector('[data-label]');
    if (label) label.textContent = btn.dataset.loadingText;
    btn.querySelector('[data-spinner]')?.classList.remove('hidden');
    btn.classList.add('opacity-80', 'cursor-wait');
  });
});

// Show the chosen file name inside upload dropzones.
document.querySelectorAll('input[type=file][data-file-label]').forEach((input) => {
  input.addEventListener('change', () => {
    const label = document.getElementById(input.dataset.fileLabel);
    if (label && input.files.length) label.textContent = input.files[0].name;
  });
});
