'use strict';
const ar=document.documentElement.lang==='ar';
const say=(a,e)=>ar?a:e;
document.querySelectorAll('[data-print]').forEach(el=>el.addEventListener('click',()=>window.print()));
document.querySelectorAll('[data-back]').forEach(el=>el.addEventListener('click',()=>history.back()));
document.querySelectorAll('form[data-confirm]').forEach(form=>form.addEventListener('submit',e=>{if(!confirm(form.dataset.confirm))e.preventDefault();}));
const photoInput=document.querySelector('[data-photo-input]');
if(photoInput){let photoUrl;photoInput.addEventListener('change',()=>{const file=photoInput.files[0],preview=document.querySelector('[data-photo-preview]');if(photoUrl)URL.revokeObjectURL(photoUrl);if(file){photoUrl=URL.createObjectURL(file);preview.src=photoUrl;preview.classList.remove('hidden');}else preview.classList.add('hidden');});}
const orderForm=document.querySelector('#order-form');
if(orderForm){
  const items=document.querySelector('#order-items'),customerSelect=document.querySelector('#customer-select');
  const customers=JSON.parse(document.querySelector('#customer-data').textContent);
  let previousCustomer=customerSelect.value;
  const amount=val=>{const n=Number(val||0);return Number.isFinite(n)?Math.round(n*100):0;};
  const fmt=v=>new Intl.NumberFormat('en-AE',{minimumFractionDigits:2,maximumFractionDigits:2}).format(v/100);
  function customer(){return customers.find(c=>String(c.id)===customerSelect.value);}
  function renumber(){
    [...items.children].forEach((item,i)=>{item.querySelector('.item-number').textContent=i+1;item.querySelectorAll('[name]').forEach(el=>el.name=el.name.replace(/^i(?:\d+|__INDEX__)_/,`i${i}_`));item.querySelector('.remove-item').disabled=items.children.length===1;});
    document.querySelector('#item-count').value=items.children.length;
  }
  function totals(){
    let subtotal=0;items.querySelectorAll('.order-item').forEach(item=>{subtotal+=amount(item.querySelector('.price-input').value)*(Number(item.querySelector('.quantity-input').value)||0);});
    const delivery=document.querySelector('#delivery-mode').value==='delivery';
    const fee=delivery?amount(document.querySelector('#delivery-fee').value):0;
    const discount=amount(document.querySelector('#discount').value),deposit=amount(document.querySelector('#deposit').value);
    document.querySelector('#subtotal').textContent=fmt(subtotal);document.querySelector('#fee-display').textContent=fmt(fee);document.querySelector('#grand-total').textContent=fmt(subtotal-discount+fee);document.querySelector('#balance').textContent=fmt(subtotal-discount+fee-deposit);
    document.querySelector('#discount').setCustomValidity(discount>subtotal?say('الخصم يتجاوز قيمة العبايات','Discount exceeds subtotal'):'');
    document.querySelector('#deposit').setCustomValidity(deposit>subtotal-discount+fee?say('الدفعة تتجاوز الإجمالي','Payment exceeds total'):'');
    document.querySelector('#delivery-fee').disabled=!delivery;
    document.querySelector('#address-field').classList.toggle('hidden',!delivery);
    document.querySelector('#delivery-address').required=delivery;
  }
  function copyMeasurements(item){
    const c=customer();if(!c)return;
    const values=JSON.parse(c.measurements);
    const suffixes=['unit','length','shoulder','bust','waist','hip','sleeve','arm','cuff'];
    suffixes.forEach(key=>{const input=item.querySelector(`[name$="_${key}"]`);if(input)input.value=values[key]||(key==='unit'?'inch':'');});
  }
  function addItem(){
    if(items.children.length>=30)return;
    const fragment=document.querySelector('#item-template').content.cloneNode(true);items.append(fragment);
    renumber();const item=items.lastElementChild;copyMeasurements(item);
    const input=item.querySelector('.model-input');let timer,version=0;
    async function lookup(){
      const current=++version,code=input.value.trim(),message=item.querySelector('.model-result'),img=item.querySelector('img'),placeholder=item.querySelector('.photo-placeholder');
      img.removeAttribute('src');img.classList.add('hidden');placeholder.classList.remove('hidden');input.setCustomValidity(say('اختر رقم موديل صحيحاً','Choose a valid model code'));
      if(!code){message.textContent=say('اكتب رقم الموديل','Enter model code');return;}
      message.textContent=say('جارٍ عرض الموديل…','Loading model…');
      try{
        const response=await fetch(`/api/model?code=${encodeURIComponent(code)}`,{headers:{Accept:'application/json'}});
        if(current!==version)return;
        if(!response.ok)throw new Error('not-found');
        const model=await response.json();if(current!==version)return;
        input.setCustomValidity('');message.classList.remove('error');message.textContent=model.title;
        item.querySelector('.price-input').value=(model.price/100).toFixed(2);
        if(model.photo_url){img.src=model.photo_url;img.classList.remove('hidden');placeholder.classList.add('hidden');}
        totals();
      }catch(error){if(current===version){message.textContent=say('لم يُعثر على الموديل. أضفه في الكتالوج أو تحقق من الاتصال.','Model not found. Add it to the catalogue or check your connection.');message.classList.add('error');item.querySelector('.price-input').value='';totals();}}
    }
    input.addEventListener('input',()=>{version++;clearTimeout(timer);input.setCustomValidity(say('انتظر التحقق من الموديل','Wait for model lookup'));timer=setTimeout(lookup,250);});
    input.addEventListener('change',()=>{clearTimeout(timer);lookup();});
    item.querySelector('.copy-measurements').addEventListener('click',()=>copyMeasurements(item));
    item.querySelector('.remove-item').addEventListener('click',()=>{if(items.children.length>1){item.remove();renumber();totals();}});
    item.querySelector('.kind-input').addEventListener('change',e=>{item.querySelector('.measure-details').open=e.target.value==='custom';});
    totals();
  }
  customerSelect.addEventListener('change',()=>{
    const hasSizes=[...items.querySelectorAll('.measurements input')].some(input=>input.value);
    if(previousCustomer&&previousCustomer!==customerSelect.value&&hasSizes){
      if(!confirm(say('تغيير العميل سيستبدل مقاسات جميع البنود بمقاسات العميل الجديد. هل تريد المتابعة؟','Changing the customer replaces every item measurement with the new customer defaults. Continue?'))){customerSelect.value=previousCustomer;return;}
      items.querySelectorAll('.measurements input').forEach(input=>input.value='');
    }
    const c=customer();if(c){document.querySelector('#delivery-address').value=c.address;items.querySelectorAll('.order-item').forEach(item=>{const inputs=[...item.querySelectorAll('.measurements input')];if(inputs.every(input=>!input.value))copyMeasurements(item);});}
    previousCustomer=customerSelect.value;
  });
  document.querySelector('#add-item').addEventListener('click',addItem);
  orderForm.addEventListener('input',totals);orderForm.addEventListener('change',totals);
  orderForm.addEventListener('submit',()=>{const button=orderForm.querySelector('button[type="submit"],.order-summary button');if(button){button.disabled=true;button.textContent=say('جارٍ حفظ الطلب…','Saving order…');}});
  window.addEventListener('pageshow',()=>{const button=orderForm.querySelector('.order-summary button');if(button&&customers.length&&document.querySelector('#model-codes').children.length){button.disabled=false;button.textContent=say('حفظ الطلب وإصدار الفاتورة','Save order & create invoice');}});
  addItem();
}
