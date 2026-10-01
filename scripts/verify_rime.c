#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <dlfcn.h>
#include "rime_api.h"
int main(int argc,char **argv) {
 if(argc!=4) return 2;
 void *lib=dlopen(argv[1],RTLD_NOW|RTLD_LOCAL);
 if(!lib) {fprintf(stderr,"%s\n",dlerror());return 3;}
 RimeApi *(*get_api)(void)=dlsym(lib,"rime_get_api"); if(!get_api)return 4;
 RimeApi *api=get_api(); RIME_STRUCT(RimeTraits,traits);
 traits.shared_data_dir=argv[2];traits.user_data_dir=argv[3];traits.app_name="rime.voicefly.package-check";traits.log_dir=argv[3];
 api->setup(&traits); api->initialize(&traits);
 if(api->start_maintenance(True))api->join_maintenance_thread();
 const char *schemas[]={"voicefly_pinyin","voicefly_flypy","voicefly_natural","voicefly_microsoft","voicefly_wubi","voicefly_wubiPinyin"};
 const char *keys[]={"nihao","nihc","nihk","nihk","wqvb","nihao"};
 int failed=0;
 for(int i=0;i<6;i++) {RimeSessionId session=api->create_session();if(!session||!api->select_schema(session,schemas[i])){failed++;printf("%s schema failed\n",schemas[i]);continue;}
 for(const char *p=keys[i];*p;p++)api->process_key(session,*p,0);
 RIME_STRUCT(RimeContext,context);if(!api->get_context(session,&context)){failed++;continue;}
 int found=0;for(int j=0;j<context.menu.num_candidates;j++)if(strstr(context.menu.candidates[j].text,"你好"))found=1;
 printf("%s candidates=%d expected_greeting=%s\n",schemas[i],context.menu.num_candidates,found?"passed":"failed");
 if(!found)failed++;api->free_context(&context);api->destroy_session(session);}
 api->finalize();dlclose(lib);return failed?1:0;
}
