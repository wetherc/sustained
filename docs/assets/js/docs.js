/* Progressive enhancement for the Sustained documentation. Nothing here is
   needed to read a page. */

(function () {
  'use strict';

  /* --- mobile navigation ------------------------------------------- */

  var toggle = document.querySelector('.nav-toggle');
  if (toggle) {
    toggle.addEventListener('click', function () {
      var open = toggle.getAttribute('aria-expanded') === 'true';
      toggle.setAttribute('aria-expanded', open ? 'false' : 'true');
    });
  }

  /* Scroll a box so one of its children is visible, without moving the page
     itself the way `scrollIntoView` does. */
  function keepInView(box, child) {
    var top = child.offsetTop;
    var bottom = top + child.offsetHeight;
    if (top < box.scrollTop) {
      box.scrollTop = top;
    } else if (bottom > box.scrollTop + box.clientHeight) {
      box.scrollTop = bottom - box.clientHeight;
    }
  }

  /* --- keep the reader's place in the sidebar ---------------------- */

  /* Each page is a fresh document, so the sidebar starts back at the top and
     a reader who clicked a link near the bottom of it loses their place.
     Carry the scroll position across, and fall back to the current entry. */

  var masthead = document.querySelector('.masthead');
  var KEY = 'sustained:nav-scroll';

  if (masthead) {
    var saved = null;
    try {
      saved = sessionStorage.getItem(KEY);
    } catch (e) {
      saved = null;
    }

    var scrolls = masthead.scrollHeight > masthead.clientHeight;

    if (saved !== null && scrolls) {
      masthead.scrollTop = parseInt(saved, 10) || 0;
    } else if (scrolls) {
      var here = masthead.querySelector('.current a');
      if (here) {
        keepInView(masthead, here);
      }
    }

    masthead.addEventListener('scroll', function () {
      try {
        sessionStorage.setItem(KEY, String(masthead.scrollTop));
      } catch (e) {
        /* private browsing, or storage is full. The sidebar still works. */
      }
    }, { passive: true });
  }

  /* --- wide tables scroll inside the column ------------------------ */

  var tables = document.querySelectorAll('.prose table');
  Array.prototype.forEach.call(tables, function (table) {
    var box = document.createElement('div');
    box.className = 'table-scroll';
    box.setAttribute('tabindex', '0');
    box.setAttribute('role', 'region');
    box.setAttribute('aria-label', 'Table, scrolls sideways');
    table.parentNode.insertBefore(box, table);
    box.appendChild(table);
  });

  /* --- mark what a statement does ---------------------------------- */

  /* A migration is safe or it is not, and the difference is a handful of
     keywords. Colour them in the SQL and console samples so a reader sees
     the destructive line before they read the paragraph about it. */

  var DESTRUCTIVE = /\b(DROP|TRUNCATE|DELETE|CASCADE)\b/g;
  var ADDITIVE = /\b(CREATE|INSERT|ADD)\b/g;

  function paint(node, pattern, className) {
    var text = node.nodeValue;
    pattern.lastIndex = 0;
    if (!pattern.test(text)) {
      return;
    }
    pattern.lastIndex = 0;

    var frag = document.createDocumentFragment();
    var last = 0;
    var match;
    while ((match = pattern.exec(text)) !== null) {
      if (match.index > last) {
        frag.appendChild(document.createTextNode(text.slice(last, match.index)));
      }
      var span = document.createElement('span');
      span.className = className;
      span.textContent = match[0];
      frag.appendChild(span);
      last = match.index + match[0].length;
    }
    if (last < text.length) {
      frag.appendChild(document.createTextNode(text.slice(last)));
    }
    node.parentNode.replaceChild(frag, node);
  }

  function paintBlock(block) {
    var walker = document.createTreeWalker(block, NodeFilter.SHOW_TEXT, null, false);
    var nodes = [];
    var node;
    while ((node = walker.nextNode()) !== null) {
      nodes.push(node);
    }
    nodes.forEach(function (textNode) {
      paint(textNode, DESTRUCTIVE, 'sql-destructive');
    });

    walker = document.createTreeWalker(block, NodeFilter.SHOW_TEXT, null, false);
    nodes = [];
    while ((node = walker.nextNode()) !== null) {
      if (!node.parentNode.classList || !node.parentNode.classList.contains('sql-destructive')) {
        nodes.push(node);
      }
    }
    nodes.forEach(function (textNode) {
      paint(textNode, ADDITIVE, 'sql-additive');
    });
  }

  var samples = document.querySelectorAll('.language-sql pre, .language-console pre');
  Array.prototype.forEach.call(samples, paintBlock);

  /* --- on this page ------------------------------------------------ */

  var aside = document.querySelector('.onthispage');
  var list = aside && aside.querySelector('ol');
  var headings = document.querySelectorAll('.prose h2[id]');

  if (aside && list && headings.length > 2) {
    Array.prototype.forEach.call(headings, function (heading) {
      var item = document.createElement('li');
      var link = document.createElement('a');
      link.href = '#' + heading.id;
      link.textContent = heading.textContent;
      item.appendChild(link);
      list.appendChild(item);
    });
    aside.hidden = false;

    var links = list.querySelectorAll('a');

    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) {
          return;
        }
        var id = entry.target.id;
        Array.prototype.forEach.call(links, function (link) {
          link.classList.toggle('active', link.getAttribute('href') === '#' + id);
        });
      });
    }, { rootMargin: '0px 0px -70% 0px' });

    Array.prototype.forEach.call(headings, function (heading) {
      observer.observe(heading);
    });
  }
})();
