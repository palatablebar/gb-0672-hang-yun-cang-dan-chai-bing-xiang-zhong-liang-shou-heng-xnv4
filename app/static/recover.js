const output=document.querySelector('#message');
document.querySelector('#print')?.addEventListener('click',()=>window.print());
const recoverButton=document.querySelector('#do-recover');
if(recoverButton)recoverButton.addEventListener('click',async()=>{
  if(recoverButton.disabled)return;
  const packageId=document.body.dataset.packageId;recoverButton.disabled=true;
  if(output)output.textContent='按现规则从封存快照重建副本…';
  try{
    const response=await fetch('/api/recover/'+encodeURIComponent(packageId),{method:'POST'});
    const data=await response.json();
    if(!response.ok)throw Error(data.detail||'恢复失败');
    location.href='/recover?package_id='+encodeURIComponent(packageId)+'&copy_id='+encodeURIComponent(data.copy_id);
  }catch(e){if(output)output.textContent=e.message;recoverButton.disabled=false;}
});
const filePicker=document.querySelector('#bundle-file');
if(filePicker)filePicker.addEventListener('change',async()=>{
  const file=filePicker.files[0];if(!file)return;
  if(output)output.textContent='校验并导入封存包…';
  try{
    const body=await file.arrayBuffer();
    const response=await fetch('/api/seals/import',{method:'POST',headers:{'Content-Type':file.type||'application/octet-stream'},body});
    const data=await response.json();
    if(!response.ok)throw Error(data.detail||'导入失败');
    location.href='/recover?package_id='+encodeURIComponent(data.package_id);
  }catch(e){if(output)output.textContent=e.message;}
});
