// Email links: open the visitor's email app, and also copy the address and show it, because on
// a computer with no email app set up a mailto: link does nothing at all when clicked.
(function () {
  document.addEventListener('click', function (e) {
    var a = e.target.closest && e.target.closest('a[href^="mailto:"]');
    if (!a) return;
    var addr = decodeURIComponent(a.getAttribute('href').slice(7).split('?')[0]);
    try { if (navigator.clipboard) navigator.clipboard.writeText(addr); } catch (err) {}
    var n = document.getElementById('email-link-note');
    if (!n) {
      n = document.createElement('div');
      n.id = 'email-link-note';
      n.setAttribute('role', 'status');
      n.style.cssText = 'position:fixed;left:50%;bottom:24px;transform:translateX(-50%);background:#12283a;color:#fff;' +
        'border:1px solid rgba(245,197,24,.45);border-radius:10px;padding:.75rem 1.1rem;font-size:.9rem;line-height:1.45;' +
        'z-index:2147483000;box-shadow:0 8px 30px rgba(0,0,0,.5);max-width:92vw;text-align:center';
      document.body.appendChild(n);
    }
    n.textContent = 'Opening your email app. If nothing opens, email us at ';
    var b = document.createElement('b');
    b.style.color = '#F5C518';
    b.textContent = addr;
    n.appendChild(b);
    n.appendChild(document.createTextNode(' (copied, ready to paste).'));
    n.style.display = 'block';
    clearTimeout(n._t);
    n._t = setTimeout(function () { n.style.display = 'none'; }, 8000);
  });
})();
