// Real-time password strength meter (Agency Registration, step 3).
// Runs entirely in the browser: the password is never sent anywhere, logged or copied out of its input.
// Markup: <input data-strength-meter="ID"> and an element #ID holding [data-strength-label],
// [data-strength-segment] x5 and [data-rule="length|upper|lower|digit|symbol"] items.
(function () {
  "use strict";

  const LEVELS = [
    { label: "Very Weak", color: "#ef4444" },
    { label: "Weak", color: "#f97316" },
    { label: "Fair", color: "#eab308" },
    { label: "Strong", color: "#86efac" },
    { label: "Very Strong", color: "#22c55e" },
  ];

  // Frequently used passwords and words; matched case-insensitively after undoing common l33t swaps.
  const COMMON = [
    "password", "passw0rd", "letmein", "welcome", "admin", "administrator", "login", "master", "secret",
    "qwerty", "azerty", "iloveyou", "monkey", "dragon", "football", "baseball", "basketball", "soccer",
    "sunshine", "princess", "shadow", "superman", "batman", "trustno1", "whatever", "freedom", "starwars",
    "computer", "internet", "changeme", "default", "access", "hello", "abc123", "123456", "1234567890",
    "111111", "000000", "654321", "michael", "jordan", "charlie", "summer", "winter", "spring", "autumn",
    "love", "god", "test", "guest", "user", "root", "emergency", "dispatch", "police", "fire", "rescue",
    "esrms", "psap", "agency", "console", "command", "security", "secure", "manila", "davao", "philippines",
  ];
  const LEET = { "0": "o", "1": "l", "3": "e", "4": "a", "5": "s", "7": "t", "8": "b", "9": "g", "@": "a", "$": "s", "!": "i", "|": "l", "+": "t" };
  const KEYBOARD_ROWS = ["qwertyuiop", "asdfghjkl", "zxcvbnm", "1234567890", "!@#$%^&*()"];

  function rules(pw) {
    return {
      length: pw.length >= 16,
      upper: /[A-Z]/.test(pw),
      lower: /[a-z]/.test(pw),
      digit: /[0-9]/.test(pw),
      symbol: /[^A-Za-z0-9]/.test(pw),
    };
  }

  function poolSize(pw) {
    let pool = 0;
    if (/[a-z]/.test(pw)) pool += 26;
    if (/[A-Z]/.test(pw)) pool += 26;
    if (/[0-9]/.test(pw)) pool += 10;
    if (/[^A-Za-z0-9]/.test(pw)) pool += 33;
    return Math.max(pool, 10);
  }

  // Marks characters that belong to a predictable pattern; they count for very little.
  function predictableMask(pw) {
    const lower = pw.toLowerCase();
    const plain = Array.from(lower, (c) => LEET[c] || c).join("");
    const mask = new Array(pw.length).fill(false);
    const mark = (from, len) => { for (let i = from; i < from + len; i++) mask[i] = true; };

    for (const word of COMMON) {
      for (const text of [lower, plain]) {
        let at = text.indexOf(word);
        while (at !== -1) { mark(at, word.length); at = text.indexOf(word, at + 1); }
      }
    }
    // Runs of one character: "aaaa", "1111".
    for (let i = 0; i < pw.length;) {
      let j = i;
      while (j + 1 < pw.length && lower[j + 1] === lower[i]) j++;
      if (j - i >= 2) mark(i, j - i + 1);
      i = j + 1;
    }
    // Ascending/descending sequences: "abcd", "4321".
    for (let i = 0; i < pw.length - 2;) {
      const step = lower.charCodeAt(i + 1) - lower.charCodeAt(i);
      let j = i + 1;
      if (step === 1 || step === -1) {
        while (j + 1 < pw.length && lower.charCodeAt(j + 1) - lower.charCodeAt(j) === step) j++;
      }
      if (j - i >= 2) { mark(i, j - i + 1); i = j + 1; } else { i++; }
    }
    // Keyboard walks: "qwer", "asdf", "!@#$" (also backwards).
    for (const row of KEYBOARD_ROWS) {
      for (const r of [row, row.split("").reverse().join("")]) {
        for (let len = r.length; len >= 4; len--) {
          for (let s = 0; s + len <= r.length; s++) {
            const chunk = r.slice(s, s + len);
            let at = lower.indexOf(chunk);
            while (at !== -1) { mark(at, len); at = lower.indexOf(chunk, at + 1); }
          }
        }
      }
    }
    // Years and dates: 1950-2039.
    const year = /(19[5-9]\d|20[0-3]\d)/g;
    let m;
    while ((m = year.exec(pw))) mark(m.index, m[0].length);
    // Repeated blocks: "abcabc", "Pass!Pass!".
    for (let size = 2; size <= pw.length / 2; size++) {
      for (let i = 0; i + 2 * size <= pw.length; i++) {
        if (lower.substr(i, size) === lower.substr(i + size, size)) mark(i + size, size);
      }
    }
    return mask;
  }

  // 0..4 (index into LEVELS), from an entropy estimate that discounts predictable parts.
  function score(pw) {
    const lower = pw.toLowerCase();
    const plain = Array.from(lower, (c) => LEET[c] || c).join("");
    if (COMMON.includes(lower) || COMMON.includes(plain)) return 0;

    const mask = predictableMask(pw);
    const bitsPerChar = Math.log2(poolSize(pw));
    let bits = 0;
    mask.forEach((predictable) => { bits += predictable ? 1 : bitsPerChar; });
    // Few distinct characters means little real variety, whatever the length.
    const distinct = new Set(lower).size;
    if (distinct < pw.length / 2) bits *= 0.6 + 0.4 * (distinct / (pw.length / 2));

    let level = bits < 28 ? 0 : bits < 40 ? 1 : bits < 60 ? 2 : bits < 85 ? 3 : 4;
    if (pw.length < 8) level = Math.min(level, 2);    // short passwords are never Strong
    if (pw.length < 12) level = Math.min(level, 3);   // Very Strong needs length, not just variety
    const predictableShare = mask.filter(Boolean).length / pw.length;
    if (predictableShare > 0.5) level = Math.min(level, 1);
    else if (predictableShare > 0.25) level = Math.min(level, 2);
    return level;
  }

  function attach(input) {
    const root = document.getElementById(input.dataset.strengthMeter);
    if (!root) return;
    const label = root.querySelector("[data-strength-label]");
    const segments = root.querySelectorAll("[data-strength-segment]");
    const items = root.querySelectorAll("[data-rule]");

    function render() {
      const pw = input.value;
      const passed = rules(pw);
      const level = pw ? score(pw) : -1;
      root.dataset.level = level;
      label.textContent = level < 0 ? "—" : LEVELS[level].label;
      label.style.color = level < 0 ? "" : LEVELS[level].color;
      segments.forEach((seg, i) => {
        seg.style.backgroundColor = i <= level ? LEVELS[level].color : "";
        seg.classList.toggle("bg-surface-container-highest", i > level);
      });
      items.forEach((item) => {
        const ok = pw.length > 0 && passed[item.dataset.rule];
        item.classList.toggle("text-primary", ok);
        item.classList.toggle("text-outline", !ok);
        item.querySelector(".material-symbols-outlined").textContent = ok ? "check_circle" : "radio_button_unchecked";
      });
      // Submission stays blocked until every rule passes; the server checks the same rules again.
      const missing = Object.keys(passed).filter((k) => !passed[k]);
      input.setCustomValidity(pw && missing.length ? "Password must meet every requirement in the checklist." : "");
    }

    input.addEventListener("input", render);
    render();
  }

  document.querySelectorAll("[data-strength-meter]").forEach(attach);
  window.ESRMSPasswordStrength = { score, rules, LEVELS };  // used by tests
})();
