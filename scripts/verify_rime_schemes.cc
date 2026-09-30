// Exercise real Rime translation in a disposable profile, without IMK or audio.
#include <rime_api.h>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <string>

int main(int argc, char** argv) {
  if (argc != 3) return 2;
  std::string user = argv[1], shared = argv[2];
  std::string staging = user + "/build";
  std::filesystem::create_directories(staging);
  RimeTraits traits;
  RIME_STRUCT_INIT(RimeTraits, traits);
  traits.user_data_dir = user.c_str();
  traits.shared_data_dir = shared.c_str();
  traits.staging_dir = staging.c_str();
  traits.log_dir = user.c_str();
  traits.app_name = "rime.voicefly.verify";
  RimeApi* api = rime_get_api();
  api->setup(&traits); api->initialize(nullptr);
  if (api->start_maintenance(true)) api->join_maintenance_thread();
  int failures = 0;
  for (const auto& scheme : {"pinyin", "flypy", "natural", "microsoft", "wubi", "wubiPinyin"}) {
    std::ifstream table(user + "/voicefly_words_" + scheme + ".txt");
    std::string line, code;
    while (std::getline(table, line)) {
      if (line.rfind("人工智能\t", 0) != 0) continue;
      auto start = line.find('\t') + 1;
      code = line.substr(start, line.find('\t', start) - start); break;
    }
    auto session = api->create_session();
    std::string id = std::string("voicefly_") + scheme;
    bool selected = api->select_schema(session, id.c_str());
    for (char key : code) api->process_key(session, key, 0);
    RimeContext context;
    RIME_STRUCT_INIT(RimeContext, context);
    bool found = false;
    if (api->get_context(session, &context)) {
      for (int i = 0; i < context.menu.num_candidates; ++i)
        if (context.menu.candidates[i].text && std::string(context.menu.candidates[i].text) == "人工智能") found = true;
      api->free_context(&context);
    }
    bool success = selected && !code.empty() && found;
    std::cout << id << " " << (success ? "PASS" : "FAIL") << std::endl;
    if (!success) ++failures;
    api->destroy_session(session);
  }
  api->finalize();
  return failures ? 1 : 0;
}
