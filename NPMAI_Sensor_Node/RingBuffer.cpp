#include "RingBuffer.h"

RingBuffer::RingBuffer() : _head(0), _tail(0), _count(0) {}

bool RingBuffer::push(const Reading& r) {
  if (_count >= RING_BUFFER_CAPACITY) {
    // Reject the new reading rather than silently overwriting the oldest
    // unsent one. For a slow trend-detection system (weeks-to-months
    // drift), the earliest points of a developing trend during a long
    // outage are often the most diagnostically useful ones - we'd
    // rather lose the newest sample than the start of a trend. If you'd
    // prefer overwrite-oldest behavior instead, advance _tail here
    // rather than returning false.
    return false;
  }
  _buf[_head] = r;
  _head = (uint16_t)((_head + 1) % RING_BUFFER_CAPACITY);
  _count++;
  return true;
}

bool RingBuffer::pop(Reading& out) {
  if (_count == 0) return false;
  out = _buf[_tail];
  _tail = (uint16_t)((_tail + 1) % RING_BUFFER_CAPACITY);
  _count--;
  return true;
}

uint16_t RingBuffer::count() const { return _count; }
bool RingBuffer::isFull() const { return _count >= RING_BUFFER_CAPACITY; }
bool RingBuffer::isEmpty() const { return _count == 0; }
