(() => {
  function start(element, text) {
    if (!element || element.classList.contains('is-processing')) return false;
    element.dataset.processingOriginalHtml = element.innerHTML;
    element.classList.add('is-processing');
    element.setAttribute('aria-busy', 'true');
    if (element.tagName === 'BUTTON') element.disabled = true;
    else element.setAttribute('aria-disabled', 'true');

    const spinner = document.createElement('span');
    spinner.className = 'processing-state-spinner';
    spinner.setAttribute('aria-hidden', 'true');
    const label = document.createElement('span');
    label.textContent = text || element.dataset.processingText || '处理中…';
    element.replaceChildren(spinner, label);
    return true;
  }

  function stop(element) {
    if (!element || !element.classList.contains('is-processing')) return;
    element.innerHTML = element.dataset.processingOriginalHtml || '';
    delete element.dataset.processingOriginalHtml;
    element.classList.remove('is-processing');
    element.removeAttribute('aria-busy');
    element.removeAttribute('aria-disabled');
    if (element.tagName === 'BUTTON') element.disabled = false;
  }

  window.ProcessingState = { start, stop };

  document.addEventListener('submit', event => {
    if (event.defaultPrevented) return;
    const form = event.target;
    const submitter = event.submitter || form.querySelector('button[type="submit"][data-processing-text], input[type="submit"][data-processing-text]');
    if (submitter && submitter.dataset.processingText) start(submitter);
  });

  document.addEventListener('click', event => {
    const link = event.target.closest('a[data-processing-text]');
    if (!link || link.hasAttribute('download') || event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    if (!start(link)) event.preventDefault();
  });

  window.addEventListener('pageshow', () => {
    document.querySelectorAll('.is-processing').forEach(stop);
  });
})();
