const output=document.querySelector('#message');
const sealButton=document.querySelector('#seal-run');
if(sealButton)sealButton.addEventListener('click',async()=>{
  if(sealButton.disabled)return;
  sealButton.disabled=true;output.textContent='封存中…';
  try{
    const response=await fetch('/api/runs/'+document.body.dataset.runId+'/seal',{method:'POST'});
    const data=await response.json();
    if(!response.ok)throw Error(data.detail);
    location.href='/recover?package_id='+encodeURIComponent(data.package_id);
  }catch(e){output.textContent=e.message;sealButton.disabled=false;}
});
