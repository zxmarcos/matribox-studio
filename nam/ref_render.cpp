// Reference renderer: runs a .nam model through the official NeuralAmpModelerCore.
//   ref_render <model.nam> <slim 0..1 or -1> <in.f32> <out.f32>
// Raw mono float32 in/out, 64-frame blocks, Reset() at the model's own sample rate
// (which prewarms the model exactly as the plugin does).
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <vector>

#include "NAM/dsp.h"
#include "NAM/get_dsp.h"
#include "NAM/slimmable.h"

int main(int argc, char** argv)
{
  if (argc != 5)
  {
    std::fprintf(stderr, "usage: ref_render model.nam slim in.f32 out.f32\n");
    return 1;
  }
  auto model = nam::get_dsp(std::filesystem::path(argv[1]));
  const double slim = std::atof(argv[2]);
  if (slim >= 0.0)
  {
    auto* s = dynamic_cast<nam::SlimmableModel*>(model.get());
    if (!s)
    {
      std::fprintf(stderr, "model is not slimmable\n");
      return 1;
    }
    s->SetSlimmableSize(slim);
  }
  const double fs = model->GetExpectedSampleRate() > 0 ? model->GetExpectedSampleRate() : 48000.0;
  const int bs = 64;
  model->Reset(fs, bs);

  FILE* fi = std::fopen(argv[3], "rb");
  FILE* fo = std::fopen(argv[4], "wb");
  if (!fi || !fo)
    return 2;
  std::vector<float> fin(bs);
  std::vector<NAM_SAMPLE> in(bs), out(bs);
  NAM_SAMPLE* ip = in.data();
  NAM_SAMPLE* op = out.data();
  size_t n;
  while ((n = std::fread(fin.data(), sizeof(float), bs, fi)) > 0)
  {
    for (size_t i = 0; i < n; i++)
      in[i] = (NAM_SAMPLE)fin[i];
    model->process(&ip, &op, (int)n);
    for (size_t i = 0; i < n; i++)
      fin[i] = (float)out[i];
    std::fwrite(fin.data(), sizeof(float), n, fo);
  }
  std::fclose(fi);
  std::fclose(fo);
  std::fprintf(stderr, "fs %.0f prewarm %d\n", fs, model->GetPrewarmSamples());
  return 0;
}
