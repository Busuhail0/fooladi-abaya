'use strict';
const customerForm = document.getElementById('customer-form');
if (customerForm) {
  const mode = customerForm.querySelector('[data-delivery-mode]');
  const address = customerForm.querySelector('[data-address]');
  const toggleAddress = () => {
    const delivery = mode.value === 'delivery';
    address.classList.toggle('hidden', !delivery);
    address.querySelector('textarea').required = delivery;
  };
  mode.addEventListener('change', toggleAddress);
  toggleAddress();
  const quantity = customerForm.querySelector('[data-quantity]');
  const estimate = customerForm.querySelector('[data-estimate]');
  const updateEstimate = () => {
    const qty = quantity ? Number(quantity.value) : 1;
    if (Number.isInteger(qty) && qty >= 1 && qty <= 10) {
      const amount = (Number(estimate.dataset.price) * qty / 100).toLocaleString('en-AE', {minimumFractionDigits:2, maximumFractionDigits:2});
      estimate.firstChild.textContent = amount + ' ';
    }
  };
  quantity?.addEventListener('input', updateEstimate);
  updateEstimate();
  const submit = customerForm.querySelector('.submit-button');
  const initialLabel = submit.textContent;
  const initiallyDisabled = submit.disabled;
  customerForm.addEventListener('submit', () => {
    submit.disabled = true;
    submit.textContent = submit.dataset.submitLabel;
  });
  window.addEventListener('pageshow', () => {
    submit.disabled = initiallyDisabled;
    submit.textContent = initialLabel;
  });
}
document.querySelector('.form-errors')?.focus();
