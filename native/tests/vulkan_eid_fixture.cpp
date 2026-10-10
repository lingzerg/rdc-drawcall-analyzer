// Headless Vulkan golden capture: real replay is the oracle for offline EIDs.
#define VK_NO_PROTOTYPES
#include <windows.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>
#include "vulkan.h"
#include "renderdoc_app.h"

static void checked(VkResult r, int line) { if(r != VK_SUCCESS) { std::fprintf(stderr, "VkResult %d at line %d\n", r, line); std::exit(1); } }
#define check(expr) checked(expr, __LINE__)

int main(int argc, char **argv)
{
  if(argc != 3 && argc != 4) return 2;
  const bool cpuWrite = argc == 4 && std::strcmp(argv[3], "cpu") == 0;
  const bool restart = argc == 4 && std::strcmp(argv[3], "restart") == 0;
  HMODULE rd = LoadLibraryA(argv[1]);
  auto getAPI = (pRENDERDOC_GetAPI)GetProcAddress(rd, "RENDERDOC_GetAPI");
  RENDERDOC_API_1_6_0 *api = nullptr;
  if(!getAPI || !getAPI(eRENDERDOC_API_Version_1_6_0, (void **)&api)) return 3;
  api->SetCaptureFilePathTemplate(argv[2]);
  HMODULE vk = LoadLibraryA("vulkan-1.dll");
  auto get = (PFN_vkGetInstanceProcAddr)GetProcAddress(vk, "vkGetInstanceProcAddr");
  auto create = (PFN_vkCreateInstance)get(nullptr, "vkCreateInstance");
  const char *extensions[] = {"VK_EXT_debug_utils"};
  VkApplicationInfo app = {VK_STRUCTURE_TYPE_APPLICATION_INFO};
  app.pApplicationName = "Offline EID golden fixture";
  app.apiVersion = VK_API_VERSION_1_0;
  VkInstanceCreateInfo ic = {VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO};
  ic.pApplicationInfo = &app;
  ic.enabledExtensionCount = 1; ic.ppEnabledExtensionNames = extensions;
  VkInstance instance;
  check(create(&ic, nullptr, &instance));
#define FN(name) auto name = (PFN_##name)get(instance, #name); if(!name) return 4
  FN(vkEnumeratePhysicalDevices); FN(vkGetPhysicalDeviceQueueFamilyProperties);
  FN(vkGetPhysicalDeviceMemoryProperties); FN(vkCreateDevice); FN(vkGetDeviceQueue);
  FN(vkCreateCommandPool); FN(vkAllocateCommandBuffers); FN(vkBeginCommandBuffer);
  FN(vkEndCommandBuffer); FN(vkCreateShaderModule); FN(vkCreatePipelineLayout);
  FN(vkCreateRenderPass); FN(vkCreateFramebuffer); FN(vkCreateGraphicsPipelines);
  FN(vkCreateComputePipelines); FN(vkCreateBuffer); FN(vkGetBufferMemoryRequirements);
  FN(vkAllocateMemory); FN(vkBindBufferMemory); FN(vkMapMemory); FN(vkUnmapMemory);
  FN(vkCmdBindPipeline); FN(vkCmdBindIndexBuffer); FN(vkCmdDraw); FN(vkCmdDrawIndexed);
  FN(vkCmdBeginRenderPass); FN(vkCmdEndRenderPass); FN(vkCmdDispatch);
  FN(vkCmdBeginDebugUtilsLabelEXT); FN(vkCmdEndDebugUtilsLabelEXT);
  FN(vkQueueSubmit); FN(vkQueueWaitIdle); FN(vkResetCommandBuffer);
  FN(vkCreateFence); FN(vkGetFenceStatus); FN(vkDestroyDevice); FN(vkDestroyInstance);
  uint32_t n = 0;
  check(vkEnumeratePhysicalDevices(instance, &n, nullptr));
  std::vector<VkPhysicalDevice> devices(n);
  check(vkEnumeratePhysicalDevices(instance, &n, devices.data()));
  if(!n) return 5;
  VkPhysicalDevice gpu = devices[0];
  vkGetPhysicalDeviceQueueFamilyProperties(gpu, &n, nullptr);
  std::vector<VkQueueFamilyProperties> families(n);
  vkGetPhysicalDeviceQueueFamilyProperties(gpu, &n, families.data());
  uint32_t family = 0;
  while(family < n && (families[family].queueFlags & (VK_QUEUE_GRAPHICS_BIT | VK_QUEUE_COMPUTE_BIT)) !=
                      (VK_QUEUE_GRAPHICS_BIT | VK_QUEUE_COMPUTE_BIT)) ++family;
  if(family == n) return 6;
  float priority = 1;
  VkDeviceQueueCreateInfo qi = {VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO};
  qi.queueFamilyIndex = family; qi.queueCount = 1; qi.pQueuePriorities = &priority;
  VkDeviceCreateInfo dc = {VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO};
  dc.queueCreateInfoCount = 1; dc.pQueueCreateInfos = &qi;
  VkDevice device;
  check(vkCreateDevice(gpu, &dc, nullptr, &device));
  VkQueue queue;
  vkGetDeviceQueue(device, family, 0, &queue);
  VkCommandPoolCreateInfo pci = {VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO};
  pci.flags = VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT; pci.queueFamilyIndex = family;
  VkCommandPool pool;
  check(vkCreateCommandPool(device, &pci, nullptr, &pool));
  VkCommandBufferAllocateInfo ai = {VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO};
  ai.commandPool = pool; ai.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY; ai.commandBufferCount = 3;
  VkCommandBuffer cb[3];
  check(vkAllocateCommandBuffers(device, &ai, cb));

  // Minimal void main SPIR-V. The vertex stage uses rasterizer discard; the
  // compute stage is a no-op. No shader compiler or external assets are needed.
  const uint32_t vs[] = {
      0x07230203, 0x00010000, 0, 5, 0, 0x00020011, 1, 0x0003000e, 0, 1,
      0x0005000f, 0, 3, 0x6e69616d, 0, 0x00020013, 1, 0x00030021, 2, 1,
      0x00050036, 1, 3, 0, 2, 0x000200f8, 4, 0x000100fd, 0x00010038};
  std::vector<uint32_t> cs(vs, vs + sizeof(vs) / 4);
  cs[11] = 5;
  cs.insert(cs.begin() + 15, {0x00060010, 3, 17, 1, 1, 1});
  VkShaderModuleCreateInfo sm = {VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO};
  sm.codeSize = sizeof(vs); sm.pCode = vs;
  VkShaderModule vertex, compute;
  check(vkCreateShaderModule(device, &sm, nullptr, &vertex));
  sm.codeSize = cs.size() * 4; sm.pCode = cs.data();
  check(vkCreateShaderModule(device, &sm, nullptr, &compute));
  VkPipelineLayoutCreateInfo li = {VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO};
  VkPipelineLayout layout;
  check(vkCreatePipelineLayout(device, &li, nullptr, &layout));
  VkSubpassDescription subpass = {}; subpass.pipelineBindPoint = VK_PIPELINE_BIND_POINT_GRAPHICS;
  VkRenderPassCreateInfo rp = {VK_STRUCTURE_TYPE_RENDER_PASS_CREATE_INFO};
  rp.subpassCount = 1; rp.pSubpasses = &subpass;
  VkRenderPass renderpass;
  check(vkCreateRenderPass(device, &rp, nullptr, &renderpass));
  VkFramebufferCreateInfo fb = {VK_STRUCTURE_TYPE_FRAMEBUFFER_CREATE_INFO};
  fb.renderPass = renderpass; fb.width = fb.height = fb.layers = 1;
  VkFramebuffer framebuffer;
  check(vkCreateFramebuffer(device, &fb, nullptr, &framebuffer));
  VkPipelineShaderStageCreateInfo stage = {VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO};
  stage.stage = VK_SHADER_STAGE_VERTEX_BIT; stage.module = vertex; stage.pName = "main";
  VkPipelineVertexInputStateCreateInfo vi = {VK_STRUCTURE_TYPE_PIPELINE_VERTEX_INPUT_STATE_CREATE_INFO};
  VkPipelineInputAssemblyStateCreateInfo ia = {VK_STRUCTURE_TYPE_PIPELINE_INPUT_ASSEMBLY_STATE_CREATE_INFO};
  ia.topology = VK_PRIMITIVE_TOPOLOGY_TRIANGLE_LIST;
  if(restart) { ia.topology = VK_PRIMITIVE_TOPOLOGY_TRIANGLE_STRIP; ia.primitiveRestartEnable = VK_TRUE; }
  VkPipelineRasterizationStateCreateInfo rs = {VK_STRUCTURE_TYPE_PIPELINE_RASTERIZATION_STATE_CREATE_INFO};
  rs.rasterizerDiscardEnable = VK_TRUE; rs.lineWidth = 1;
  VkGraphicsPipelineCreateInfo gp = {VK_STRUCTURE_TYPE_GRAPHICS_PIPELINE_CREATE_INFO};
  gp.stageCount = 1; gp.pStages = &stage; gp.layout = layout; gp.renderPass = renderpass;
  gp.pVertexInputState = &vi; gp.pInputAssemblyState = &ia; gp.pRasterizationState = &rs;
  VkPipeline graphics, comp;
  check(vkCreateGraphicsPipelines(device, VK_NULL_HANDLE, 1, &gp, nullptr, &graphics));
  VkComputePipelineCreateInfo cp = {VK_STRUCTURE_TYPE_COMPUTE_PIPELINE_CREATE_INFO};
  stage.stage = VK_SHADER_STAGE_COMPUTE_BIT; stage.module = compute;
  cp.stage = stage; cp.layout = layout;
  check(vkCreateComputePipelines(device, VK_NULL_HANDLE, 1, &cp, nullptr, &comp));

  VkBufferCreateInfo bi = {VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO};
  bi.size = 12; bi.usage = VK_BUFFER_USAGE_INDEX_BUFFER_BIT;
  VkBuffer indices;
  check(vkCreateBuffer(device, &bi, nullptr, &indices));
  VkMemoryRequirements req;
  vkGetBufferMemoryRequirements(device, indices, &req);
  VkPhysicalDeviceMemoryProperties memory;
  vkGetPhysicalDeviceMemoryProperties(gpu, &memory);
  uint32_t mt = 0;
  while(mt < memory.memoryTypeCount && (!(req.memoryTypeBits & (1U << mt)) ||
      (memory.memoryTypes[mt].propertyFlags & (VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT)) !=
      (VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT))) ++mt;
  if(mt == memory.memoryTypeCount) return 7;
  VkMemoryAllocateInfo ma = {VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO};
  ma.allocationSize = req.size; ma.memoryTypeIndex = mt;
  VkDeviceMemory mem;
  check(vkAllocateMemory(device, &ma, nullptr, &mem));
  check(vkBindBufferMemory(device, indices, mem, 0));
  void *mapped;
  check(vkMapMemory(device, mem, 0, VK_WHOLE_SIZE, 0, &mapped));
  uint16_t values[] = {0, 1, 2, 0, 2, 3};
  if(restart) values[3] = 0xffff;
  std::memcpy(mapped, values, sizeof(values));
  if(!cpuWrite) vkUnmapMemory(device, mem);
  VkFenceCreateInfo fc = {VK_STRUCTURE_TYPE_FENCE_CREATE_INFO}; fc.flags = VK_FENCE_CREATE_SIGNALED_BIT;
  VkFence fence; check(vkCreateFence(device, &fc, nullptr, &fence));

  VkSubmitInfo warmup = {VK_STRUCTURE_TYPE_SUBMIT_INFO};
  check(vkQueueSubmit(queue, 1, &warmup, VK_NULL_HANDLE));
  check(vkQueueWaitIdle(queue));
  auto captureDevice = RENDERDOC_DEVICEPOINTER_FROM_VKINSTANCE(instance);
  api->StartFrameCapture(captureDevice, nullptr);
  if(!api->IsFrameCapturing()) return 8;
  VkCommandBufferBeginInfo begin = {VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO};
  begin.flags = VK_COMMAND_BUFFER_USAGE_SIMULTANEOUS_USE_BIT;
  VkRenderPassBeginInfo pass = {VK_STRUCTURE_TYPE_RENDER_PASS_BEGIN_INFO};
  pass.renderPass = renderpass; pass.framebuffer = framebuffer; pass.renderArea.extent = {1, 1};
  auto draw = [&](VkCommandBuffer cmd, bool indexed) {
    vkCmdBeginRenderPass(cmd, &pass, VK_SUBPASS_CONTENTS_INLINE);
    vkCmdBindPipeline(cmd, VK_PIPELINE_BIND_POINT_GRAPHICS, graphics);
    if(indexed) {
      vkCmdBindIndexBuffer(cmd, indices, 0, VK_INDEX_TYPE_UINT16);
      vkCmdDrawIndexed(cmd, 6, 2, 0, 0, 0);
    } else vkCmdDraw(cmd, 3, 1, 0, 0);
    vkCmdEndRenderPass(cmd);
  };
  // Interleaved recording, then reverse submission order, then reuse and re-record.
  check(vkBeginCommandBuffer(cb[0], &begin));
  VkDebugUtilsLabelEXT label = {VK_STRUCTURE_TYPE_DEBUG_UTILS_LABEL_EXT}; label.pLabelName = "First recorded";
  vkCmdBeginDebugUtilsLabelEXT(cb[0], &label);
  check(vkBeginCommandBuffer(cb[1], &begin));
  draw(cb[1], true);
  check(vkEndCommandBuffer(cb[1]));
  draw(cb[0], false);
  vkCmdEndDebugUtilsLabelEXT(cb[0]);
  check(vkEndCommandBuffer(cb[0]));
  check(vkBeginCommandBuffer(cb[2], &begin));
  vkCmdBindPipeline(cb[2], VK_PIPELINE_BIND_POINT_COMPUTE, comp);
  vkCmdDispatch(cb[2], 1, 1, 1);
  check(vkEndCommandBuffer(cb[2]));
  check(vkGetFenceStatus(device, fence));
  VkCommandBuffer order[] = {cb[2], cb[1], cb[0]};
  VkSubmitInfo submit[2] = {{VK_STRUCTURE_TYPE_SUBMIT_INFO}, {VK_STRUCTURE_TYPE_SUBMIT_INFO}};
  submit[0].commandBufferCount = 3; submit[0].pCommandBuffers = order;
  // Second submit info intentionally has zero command buffers.
  check(vkQueueSubmit(queue, 2, submit, VK_NULL_HANDLE));
  check(vkQueueWaitIdle(queue));
  submit[0].commandBufferCount = 1; submit[0].pCommandBuffers = &cb[1];
  check(vkQueueSubmit(queue, 1, submit, VK_NULL_HANDLE));
  check(vkQueueWaitIdle(queue));
  check(vkResetCommandBuffer(cb[0], 0));
  if(cpuWrite) std::memset(mapped, 0, sizeof(values));
  check(vkBeginCommandBuffer(cb[0], &begin));
  draw(cb[0], true);
  check(vkEndCommandBuffer(cb[0]));
  submit[0].pCommandBuffers = &cb[0];
  check(vkQueueSubmit(queue, 1, submit, VK_NULL_HANDLE));
  check(vkQueueWaitIdle(queue));
  if(!api->EndFrameCapture(captureDevice, nullptr)) return 9;
  uint32_t size = 0;
  api->GetCapture(0, nullptr, &size, nullptr);
  std::vector<char> path(size + 1);
  if(!api->GetCapture(0, path.data(), &size, nullptr)) return 10;
  std::printf("Capture: %s\n", path.data());
  // The short-lived fixture lets process teardown release its resources.
  return 0;
}
