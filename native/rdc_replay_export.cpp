#include <windows.h>
#include <fstream>
#include <iostream>
#include <map>
#include <set>
#include <string>
#include <unordered_set>
#include "renderdoc_replay.h"
#include "pipestate.inl"
#include "export_version.h"

template <> rdcstr DoStringise(const unsigned int &value)
{ return std::to_string(value).c_str(); }
template <> rdcstr DoStringise(const ResultCode &value)
{ return std::to_string(uint32_t(value)).c_str(); }

REPLAY_PROGRAM_MARKER();

static std::string utf8(const wchar_t *s)
{
  int size = WideCharToMultiByte(CP_UTF8, 0, s, -1, nullptr, 0, nullptr, nullptr);
  std::string result(size, '\0');
  WideCharToMultiByte(CP_UTF8, 0, s, -1, &result[0], size, nullptr, nullptr);
  result.pop_back();
  return result;
}

static std::string csv(const std::string &s)
{
  std::string out = "\"";
  for(char c : s) { if(c == '"') out += '"'; out += c; }
  return out + '"';
}

static std::string join(const std::set<std::string> &values)
{
  std::string out;
  for(const auto &value : values) { if(!out.empty()) out += '\n'; out += value; }
  return out;
}

static void writeActions(IReplayController *controller, std::ostream &out,
                         const rdcarray<ActionDescription> &actions, const std::string &path,
                         const std::map<ResourceId, std::string> &names,
                         const std::set<ResourceId> &textures)
{
  for(const auto &action : actions)
  {
    std::string name = action.GetName(controller->GetStructuredFile()).c_str();
    bool draw = bool(action.flags & ActionFlags::Drawcall) &&
                !bool(action.flags & ActionFlags::MultiAction);
    bool dispatch = bool(action.flags & ActionFlags::Dispatch) &&
                    !bool(action.flags & ActionFlags::MultiAction);
    if(draw || dispatch)
    {
      controller->SetFrameEvent(action.eventId, true);
      const PipeState &pipe = controller->GetPipelineState();
      std::set<std::string> textureNames, meshNames;
      auto resourceName = [&](ResourceId id) {
        auto it = names.find(id);
        return it == names.end() ? std::string() : it->second;
      };
      for(const auto &descriptor : pipe.GetAllUsedDescriptors(false))
      {
        ResourceId id = descriptor.descriptor.resource;
        if(textures.count(id)) textureNames.insert(resourceName(id));
      }
      for(const auto &buffer : pipe.GetVBuffers())
      {
        std::string bufferName = resourceName(buffer.resourceId);
        if(!bufferName.empty()) meshNames.insert(bufferName);
      }
      bool indexed = bool(action.flags & ActionFlags::Indexed);
      uint32_t instances = bool(action.flags & ActionFlags::Instanced) ? action.numInstances : 1;
      std::string unique;
      if(draw && (instances == 0 || action.numIndices == 0)) unique = "0";
      else if(draw && !indexed) unique = std::to_string(action.numIndices);
      else if(draw)
      {
        BoundVBuffer ib = pipe.GetIBuffer();
        uint64_t bytes = uint64_t(action.numIndices) * ib.byteStride;
        // A capped or incomplete read stays unknown, never zero.
        if((ib.byteStride == 1 || ib.byteStride == 2 || ib.byteStride == 4) &&
           ib.resourceId != ResourceId() && bytes <= 64ULL * 1024 * 1024)
        {
          bytebuf data = controller->GetBufferData(
              ib.resourceId, ib.byteOffset + uint64_t(action.indexOffset) * ib.byteStride, bytes);
          if(data.size() == bytes)
          {
            std::unordered_set<uint32_t> indices;
            uint32_t mask = ib.byteStride == 4 ? 0xffffffffU : (1U << (8 * ib.byteStride)) - 1;
            for(uint32_t i = 0; i < action.numIndices; ++i)
            {
              uint32_t value = 0;
              memcpy(&value, data.data() + uint64_t(i) * ib.byteStride, ib.byteStride);
              if(pipe.IsRestartEnabled() && value == (pipe.GetRestartIndex() & mask)) continue;
              // Constant baseVertex does not change the number of distinct indices.
              indices.insert(value);
            }
            unique = std::to_string(indices.size());
          }
        }
      }
      uint32_t chunk = action.events.empty() ? APIEvent::NoChunk : action.events.back().chunkIndex;
      out << action.eventId << ',' << action.actionId << ',' << chunk << ','
          << uint32_t(action.flags) << ',' << action.numIndices << ',' << action.numInstances << ','
          << indexed << ',' << action.indexOffset << ',' << action.baseVertex << ','
          << action.vertexOffset << ',' << action.instanceOffset << ',' << action.drawIndex << ','
          << csv(name) << ',' << csv(path) << ',' << csv(join(meshNames)) << ','
          << csv(join(textureNames)) << ',' << unique << ',' << (draw ? "draw" : "dispatch") << '\n';
    }
    std::string childPath = path;
    if(!name.empty()) childPath += (path.empty() ? "" : "/") + name;
    writeActions(controller, out, action.children, childPath, names, textures);
  }
}

int wmain(int argc, wchar_t **argv)
{
  if(argc != 3) { std::cerr << "Usage: rdc_replay_export capture.rdc output.csv\n"; return 2; }
  if(std::string(RENDERDOC_GetCommitHash()) != EXPECTED_RENDERDOC_COMMIT)
  {
    std::cerr << "RenderDoc DLL revision differs from exporter headers. Rebuild exporter.\n";
    return 1;
  }
  RENDERDOC_InitialiseReplay(GlobalEnvironment(), {});
  int code = 0;
  {
    ICaptureFile *file = RENDERDOC_OpenCaptureFile();
    ResultDetails result = file->OpenFile(utf8(argv[1]).c_str(), "rdc", nullptr);
    IReplayController *controller = nullptr;
    if(result.OK()) rdctie(result, controller) = file->OpenCapture(ReplayOptions(), nullptr);
    if(!result.OK()) { std::cerr << result.Message().c_str() << '\n'; code = 1; }
    else
    {
      std::ofstream out(argv[2], std::ios::binary);
      if(!out) { std::cerr << "Cannot create output\n"; code = 1; }
      else
      {
        std::map<ResourceId, std::string> names;
        std::set<ResourceId> textures;
        for(const auto &r : controller->GetResources()) names[r.resourceId] = r.name.c_str();
        for(const auto &t : controller->GetTextures()) textures.insert(t.resourceId);
        out << "eventId,actionId,mainChunkIndex,flags,numIndices,numInstances,indexed,indexOffset,"
               "baseVertex,vertexOffset,instanceOffset,drawIndex,name,path,meshNames,textureNames,"
               "uniqueVertices,kind\n";
        writeActions(controller, out, controller->GetRootActions(), "", names, textures);
        out.flush();
        if(!out) code = 1;
      }
      controller->Shutdown();
    }
    file->Shutdown();
  }
  RENDERDOC_ShutdownReplay();
  return code;
}
