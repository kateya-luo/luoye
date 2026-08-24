const ctrls = window.clearMeetingDesktop?.windowControls;
document.querySelector('#min')?.addEventListener('click', () => ctrls?.minimize());
document.querySelector('#max')?.addEventListener('click', () => ctrls?.maximize());
document.querySelector('#close')?.addEventListener('click', () => ctrls?.close());

document.querySelector('#setup').addEventListener('submit', async (event) => {
  event.preventDefault();
  const error = document.querySelector('#error');
  error.textContent = '';
  try { await window.clearMeetingDesktop.saveServerUrl(document.querySelector('#server').value); }
  catch (requestError) { error.textContent = requestError.message; }
});
