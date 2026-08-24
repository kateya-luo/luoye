#include <stdio.h>
#include <string.h>

#include "live_protocol.h"

static int failures;
#define CHECK(x) do { if (!(x)) { \
  printf("FAIL line %d: %s\n", __LINE__, #x); failures++; \
} } while (0)

int main(void) {
  CHECK(luoye_live_cursor_accept(0, 0, 163840, 1, 163840));
  CHECK(luoye_live_cursor_accept(8, 300000, 327680, 9, 320000));
  CHECK(!luoye_live_cursor_accept(8, 300000, 327680, 8, 327680));
  CHECK(!luoye_live_cursor_accept(8, 300000, 327680, 7, 327680));
  CHECK(!luoye_live_cursor_accept(8, 300000, 327680, 9, 327681));
  CHECK(!luoye_live_cursor_accept(8, 300000, 327680, 9, 299999));

  char text[32];
  CHECK(luoye_live_set_text(text, sizeof(text), "hello"));
  CHECK(strcmp(text, "hello") == 0);
  CHECK(luoye_live_set_text(text, sizeof(text), "字幕"));
  CHECK(!luoye_live_set_text(text, 4, "字幕"));
  CHECK(!luoye_live_set_text(text, sizeof(text), "line\nbreak"));
  CHECK(!luoye_live_set_text(text, sizeof(text), "\xF0\x9F\x98\x80"));
  CHECK(!luoye_live_set_text(text, sizeof(text), "\xE4\xB8"));

  char rolling[10] = "one";
  CHECK(luoye_live_append_text(rolling, sizeof(rolling), "two"));
  CHECK(strcmp(rolling, "one two") == 0);
  CHECK(luoye_live_append_text(rolling, sizeof(rolling), "xyz"));
  CHECK(strcmp(rolling, "two xyz") == 0);
  char utf8_rolling[10] = "";
  CHECK(luoye_live_append_text(utf8_rolling, sizeof(utf8_rolling),
      "\xE4\xB8\x80\xE4\xBA\x8C\xE4\xB8\x89\xE5\x9B\x9B"));
  CHECK(strcmp(utf8_rolling,
      "\xE4\xBA\x8C\xE4\xB8\x89\xE5\x9B\x9B") == 0);
  CHECK(!luoye_live_append_text(utf8_rolling, sizeof(utf8_rolling),
                                "line\nbreak"));

  luoye_live_caption_cache_t captions;
  luoye_live_caption_cache_init(&captions);
  CHECK(luoye_live_caption_upsert(&captions, "seg-a", "第一句"));
  CHECK(luoye_live_caption_upsert(&captions, "seg-b", "第二句"));
  /* Speaker-only revisions carry the same resource and must be pixel-no-op. */
  CHECK(!luoye_live_caption_upsert(&captions, "seg-a", "第一句"));
  char caption_text[64];
  CHECK(luoye_live_caption_build(&captions, caption_text,
                                 sizeof(caption_text)));
  CHECK(strcmp(caption_text, "第一句 第二句") == 0);
  /* A real correction updates in place without moving the old sentence to
     the newest position. */
  CHECK(luoye_live_caption_upsert(&captions, "seg-a", "第一句修正"));
  CHECK(luoye_live_caption_build(&captions, caption_text,
                                 sizeof(caption_text)));
  CHECK(strcmp(caption_text, "第一句修正 第二句") == 0);

  char id[16], value[16];
  for (int i = 0; i < LUOYE_LIVE_CAPTION_CACHE_ITEMS + 1; ++i) {
    snprintf(id, sizeof(id), "seg-%02d", i);
    snprintf(value, sizeof(value), "line-%02d", i);
    CHECK(luoye_live_caption_upsert(&captions, id, value));
  }
  /* seg-a has fallen out of the display cache, but remains in the seen set;
     a late speaker update cannot resurrect it as the newest caption. */
  CHECK(!luoye_live_caption_upsert(&captions, "seg-a", "第一句修正"));

  char caption_512[LUOYE_LIVE_CAPTION_TEXT_BYTES];
  memset(caption_512, 'x', sizeof(caption_512) - 1U);
  caption_512[sizeof(caption_512) - 1U] = '\0';
  CHECK(luoye_live_caption_upsert(&captions, "seg-512", caption_512));
  char caption_513[LUOYE_LIVE_CAPTION_TEXT_BYTES + 1U];
  memset(caption_513, 'y', sizeof(caption_513) - 1U);
  caption_513[sizeof(caption_513) - 1U] = '\0';
  CHECK(!luoye_live_caption_upsert(&captions, "seg-too-long", caption_513));

  char query[256];
  CHECK(luoye_live_query(query, sizeof(query), 4294967295U, 123U,
                         120U, 121U, 122U, 119U));
  CHECK(strcmp(query,
      "?after_revision=4294967295&include_partial=1&after_display_revision=123"
      "&after_caption_revision=120&after_speaker_revision=121"
      "&after_translation_revision=122&after_summary_revision=119") == 0);
  CHECK(!luoye_live_query(query, 16, 100, 200, 1, 2, 3, 4));

  printf(failures ? "%d live-protocol checks failed\n"
                  : "live-protocol checks passed\n", failures);
  return failures ? 1 : 0;
}
