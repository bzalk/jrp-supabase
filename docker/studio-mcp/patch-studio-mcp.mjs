import { readFileSync, writeFileSync } from 'node:fs'
import { globSync } from 'node:fs'

const files = globSync('/app/apps/studio/.next/server/chunks/*.js')
const candidates = files.filter((file) => {
  const source = readFileSync(file, 'utf8')
  return (
    source.includes('createSupabaseMcpServer') &&
    source.includes('StreamableHTTPServerTransport') &&
    source.includes('tools/list') === false
  )
})

const handlerFile =
  candidates.find((file) => {
    const source = readFileSync(file, 'utf8')
    return source.includes('projectId:c.DEFAULT_PROJECT.ref,features:l,readOnly:d')
  }) || null

if (!handlerFile) {
  throw new Error('Unable to find compiled Studio MCP handler chunk to patch')
}

const source = readFileSync(handlerFile, 'utf8')
if (source.includes('jrp_list_branches')) {
  console.log(`Studio MCP handler already patched: ${handlerFile}`)
  process.exit(0)
}

const needle =
  'let r=(0,s.createSupabaseMcpServer)({platform:p,projectId:c.DEFAULT_PROJECT.ref,features:l,readOnly:d}),o=new a.StreamableHTTPServerTransport'

const patch = String.raw`
let r=(0,s.createSupabaseMcpServer)({platform:p,projectId:c.DEFAULT_PROJECT.ref,features:l,readOnly:d});
((server)=>{
  const handlers=server?._requestHandlers;
  if(!handlers?.get||!handlers?.set)return;

  const originalList=handlers.get("tools/list");
  const originalCall=handlers.get("tools/call");
  if(!originalList||!originalCall)return;

  const INITIAL_SWITCH_POLL_DELAY_MS=12000;
  const SWITCH_POLL_INTERVAL_MS=5000;
  const SWITCH_MAX_POLL_ATTEMPTS=24;
  const TRANSIENT_GATEWAY_ERRORS=["502 Bad Gateway","503 Service Unavailable","504 Gateway Timeout","connection reset"];

  function retryPolicy(jobId,initialDelayMs=SWITCH_POLL_INTERVAL_MS){
    return {
      initial_wait_ms:initialDelayMs,
      poll_interval_ms:SWITCH_POLL_INTERVAL_MS,
      max_poll_attempts:SWITCH_MAX_POLL_ATTEMPTS,
      transient_gateway_errors:TRANSIENT_GATEWAY_ERRORS,
      on_transient_gateway_error:"Do not treat this as switch failure. The local gateway is probably restarting. Wait 10000-15000 ms, then retry the same tool call.",
      suggested_next_call:jobId?{tool:"jrp_get_branch_job",arguments:{id:jobId}}:null
    };
  }

  const toolSpecs={
    jrp_list_branches:{
      name:"jrp_list_branches",
      description:"List local sync-api branch snapshots, including the active branch, branch metadata, dump sizes, and storage sizes.",
      inputSchema:{type:"object",properties:{},additionalProperties:false},
      annotations:{title:"List local branches",readOnlyHint:true,destructiveHint:false,idempotentHint:true,openWorldHint:false}
    },
    jrp_switch_branches:{
      name:"jrp_switch_branches",
      description:"Switch the active local sync-api branch by restoring a saved branch snapshot into the local Supabase runtime. This returns as soon as the switch job is queued by default. After calling it through public /mcp, wait at least 12 seconds before polling because switching may restart Kong and cause temporary Bad Gateway responses.",
      inputSchema:{
        type:"object",
        properties:{
          name:{type:"string",description:"Branch name to switch to."},
          autosave:{type:"boolean",default:true,description:"Save the current active branch before restoring the target branch."},
          wait:{type:"boolean",default:false,description:"Wait for the async switch job to finish before returning. Leave false when calling through public /mcp; instead wait at least 12 seconds, then call jrp_get_branch_job."},
          timeout_ms:{type:"number",default:180000,description:"Maximum wait time in milliseconds when wait is true."}
        },
        required:["name"],
        additionalProperties:false
      },
      annotations:{title:"Switch local branch",readOnlyHint:false,destructiveHint:true,idempotentHint:false,openWorldHint:false}
    },
    jrp_get_branch_job:{
      name:"jrp_get_branch_job",
      description:"Wait for a sync-api branch job to finish and return the final status. Use this with the job id returned by jrp_switch_branches. This waits by default; set wait:false only if you need a single immediate status snapshot. If public /mcp returns Bad Gateway before this tool responds, wait 10-15 seconds and retry because Kong may still be restarting.",
      inputSchema:{
        type:"object",
        properties:{
          id:{type:"string",description:"Job id returned by jrp_switch_branches."},
          wait:{type:"boolean",default:true,description:"Wait until the job status is succeeded or failed before returning."},
          timeout_ms:{type:"number",default:180000,description:"Maximum time to wait for completion."},
          poll_interval_ms:{type:"number",default:5000,description:"Delay between status checks while waiting."}
        },
        required:["id"],
        additionalProperties:false
      },
      annotations:{title:"Get branch job status",readOnlyHint:true,destructiveHint:false,idempotentHint:true,openWorldHint:false}
    }
  };

  function getSyncConfig(){
    const base=(process.env.SYNC_API_MCP_BASE_URL||process.env.SYNC_API_BASE_URL||"http://sync-api:8080").replace(/\/+$/,"");
    const token=process.env.SYNC_API_MCP_TOKEN||process.env.SYNC_API_TOKEN;
    if(!token)throw Error("SYNC_API_TOKEN is not configured for Studio MCP");
    return{base,token};
  }

  async function syncRequest(path,options={}){
    const{base,token}=getSyncConfig();
    const response=await fetch(base+path,{
      method:options.method||"GET",
      headers:{Authorization:"Bearer "+token,Accept:"application/json",...(options.body?{"Content-Type":"application/json"}:{})},
      body:options.body?JSON.stringify(options.body):undefined,
      signal:AbortSignal.timeout(options.timeout||10000)
    });
    const text=await response.text();
    let data;
    try{data=text?JSON.parse(text):{}}catch{data={raw:text}}
    if(!response.ok)throw Error("Sync API request failed ("+response.status+"): "+(data.error||text));
    return data;
  }

  async function listBranches(){
    return await syncRequest("/v1/branches");
  }

  async function getBranchJob(args){
    const id=args?.id;
    if(typeof id!=="string"||!id.trim())throw Error("id is required");
    const jobId=id.trim();
    const wait=args?.wait!==false;
    let timeoutMs=Number(args?.timeout_ms??180000);
    Number.isFinite(timeoutMs)&&timeoutMs>0||(timeoutMs=180000);
    let pollIntervalMs=Number(args?.poll_interval_ms??SWITCH_POLL_INTERVAL_MS);
    Number.isFinite(pollIntervalMs)&&pollIntervalMs>0||(pollIntervalMs=SWITCH_POLL_INTERVAL_MS);
    const deadline=Date.now()+timeoutMs;
    let jobResponse;
    for(;;){
      jobResponse=await syncRequest("/v1/jobs/"+encodeURIComponent(jobId),{timeout:10000});
      const status=jobResponse?.job?.status;
      if(!wait||["succeeded","failed"].includes(status))break;
      if(Date.now()>deadline)throw Error("Timed out waiting for branch job "+jobId);
      await new Promise((resolve)=>setTimeout(resolve,pollIntervalMs));
    }
    const status=jobResponse?.job?.status;
    return {
      ...jobResponse,
      done:["succeeded","failed"].includes(status),
      waited:wait,
      retry_policy:retryPolicy(jobId),
      next:status==="succeeded"
        ?{tool:"jrp_list_branches",reason:"Confirm the active branch after the completed switch."}
        :status==="failed"
          ?null
          :{tool:"jrp_get_branch_job",arguments:{id:jobId},wait_before_retry_ms:SWITCH_POLL_INTERVAL_MS,reason:"Wait before polling again. Branch switching can restart the gateway, so transient Bad Gateway responses should be retried patiently."}
    };
  }

  async function switchBranch(args){
    const rawName=args?.name;
    if(typeof rawName!=="string"||!rawName.trim())throw Error("name is required");
    const name=rawName.trim();
    const autosave=args?.autosave!==false;
    const wait=args?.wait===true;
    let timeoutMs=Number(args?.timeout_ms??180000);
    Number.isFinite(timeoutMs)&&timeoutMs>0||(timeoutMs=180000);

    const response=await syncRequest("/v1/branches/"+encodeURIComponent(name)+"/switch",{method:"POST",body:{autosave},timeout:10000});
    const jobId=response?.job?.id;
    if(!jobId)throw Error("Switch response did not include a job id");

    if(!wait)return {
      queued:true,
      target_branch:name,
      autosave,
      job:response?.job,
      retry_policy:retryPolicy(jobId,INITIAL_SWITCH_POLL_DELAY_MS),
      next:{
        tool:"jrp_get_branch_job",
        arguments:{id:jobId,wait:true,timeout_ms:180000,poll_interval_ms:SWITCH_POLL_INTERVAL_MS},
        wait_before_call_ms:INITIAL_SWITCH_POLL_DELAY_MS,
        then:"After the job succeeds, call jrp_list_branches to confirm the active branch."
      },
      note:"The switch job was queued. Be patient before polling: wait at least 12 seconds. Bad Gateway from public /mcp during this window usually means Kong is restarting, not that the switch failed."
    };

    const deadline=Date.now()+timeoutMs;
    let jobResponse=response;
    for(;;){
      jobResponse=await syncRequest("/v1/jobs/"+encodeURIComponent(jobId),{timeout:10000});
      if(["succeeded","failed"].includes(jobResponse?.job?.status))break;
      if(Date.now()>deadline)throw Error("Timed out waiting for branch switch job "+jobId);
      await new Promise((resolve)=>setTimeout(resolve,SWITCH_POLL_INTERVAL_MS));
    }
    return{...response,job:jobResponse.job,branch_state:await listBranches()};
  }

  handlers.set("tools/list",async(...args)=>{
    const result=await originalList(...args);
    result.tools=result.tools||[];
    const names=new Set(result.tools.map((tool)=>tool.name));
    for(const spec of Object.values(toolSpecs))if(!names.has(spec.name))result.tools.push(spec);
    return result;
  });

  handlers.set("tools/call",async(request,...args)=>{
    const name=request?.params?.name;
    if(!Object.hasOwn(toolSpecs,name))return originalCall(request,...args);
    try{
      const result=name==="jrp_list_branches"
        ?await listBranches()
        :name==="jrp_switch_branches"
          ?await switchBranch(request?.params?.arguments||{})
          :await getBranchJob(request?.params?.arguments||{});
      return{content:[{type:"text",text:JSON.stringify(result,null,2)}]};
    }catch(error){
      return{isError:true,content:[{type:"text",text:JSON.stringify({error:{name:error?.name,message:error?.message}},null,2)}]};
    }
  });
})(r);
let o=new a.StreamableHTTPServerTransport`

if (!source.includes(needle)) {
  throw new Error(`Expected MCP handler code was not found in ${handlerFile}`)
}

writeFileSync(handlerFile, source.replace(needle, patch))
console.log(`Patched Studio MCP handler: ${handlerFile}`)
