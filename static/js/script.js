function previewAnalysis(){
    const text=(document.getElementById('complaintText')?.value||'').toLowerCase();
    const selected=document.getElementById('category')?.value||'Other';
    const rules={
        'Road Damage':['road','pothole','damage','broken road'],
        'Garbage':['garbage','waste','trash','dump'],
        'Water Leakage':['water','leak','pipe','leakage'],
        'Street Light':['street light','lamp','light'],
        'Drainage':['drain','drainage','sewage'],
        'Electricity':['electricity','power','wire','current'],
        'Traffic':['traffic','signal','vehicle','jam'],
        'Fire':['fire','smoke','burning']
    };
    let keys=[];let suggested=selected;
    Object.keys(rules).forEach(category=>rules[category].forEach(word=>{
        if(text.includes(word)){keys.push(word);if(selected==='Other')suggested=category;}
    }));
    const high=['fire','accident','danger','emergency','injury','electric shock'];
    const medium=['broken','leak','blocked','damage','traffic'];
    const priority=high.some(w=>text.includes(w))?'High':medium.some(w=>text.includes(w))?'Medium':'Low';
    document.getElementById('keywords').textContent=[...new Set(keys)].join(', ')||'None found';
    document.getElementById('suggested').textContent=suggested||'Other';
    document.getElementById('priority').textContent=priority;
    document.getElementById('analysis').classList.remove('d-none');
}

function useLocation(){
    const input=document.getElementById('location');
    const help=document.getElementById('locationHelp');
    if(!navigator.geolocation){help.textContent='Location is not supported by this browser. Please type the area.';return;}
    help.textContent='Getting your location...';
    navigator.geolocation.getCurrentPosition(
        position=>{
            const lat=position.coords.latitude.toFixed(6);
            const lon=position.coords.longitude.toFixed(6);
            input.value=`Latitude ${lat}, Longitude ${lon}`;
            help.textContent='Location added. You can replace it with a landmark if needed.';
        },
        ()=>{help.textContent='Could not access location. Please type your area or landmark.';}
    );
}
