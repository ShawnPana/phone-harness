#import <Cocoa/Cocoa.h>
#import <ApplicationServices/ApplicationServices.h>
#import <dlfcn.h>
#import <unistd.h>
#import <time.h>
#import <errno.h>
// Experimental, build-dependent private event ABI. No global post or cursor warp.
typedef struct {uint32_t high,low;} PSN;
static void die(const char *s){fprintf(stderr,"%s\n",s);exit(1);}
static double coordinate(const char *s){char *end;errno=0;double x=strtod(s,&end);if(errno||end==s||*end||!isfinite(x)||x<0||x>1)die("Coordinates must be finite numbers in [0,1].");return x;}
static BOOL windowBoundsOnScreen(CGWindowID wid,CGRect *bounds){
 NSArray *windows=CFBridgingRelease(CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly,kCGNullWindowID));
 for(NSDictionary *w in windows)if([w[(__bridge NSString *)kCGWindowNumber] unsignedIntValue]==wid){CGRect r;if(!CGRectMakeWithDictionaryRepresentation((__bridge CFDictionaryRef)w[(__bridge NSString *)kCGWindowBounds],&r))return NO;if(bounds)*bounds=r;return YES;}
 return NO;
}
static BOOL cursorOverWindow(CGWindowID wid){
 CGEventRef sample=CGEventCreate(NULL);CGPoint point=CGEventGetLocation(sample);CFRelease(sample);
 NSArray *windows=CFBridgingRelease(CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly|kCGWindowListExcludeDesktopElements,kCGNullWindowID));
 for(NSDictionary *w in windows){CGRect r;if([w[(__bridge NSString *)kCGWindowAlpha] doubleValue]<=0||!CGRectMakeWithDictionaryRepresentation((__bridge CFDictionaryRef)w[(__bridge NSString *)kCGWindowBounds],&r)||!CGRectContainsPoint(r,point))continue;return [w[(__bridge NSString *)kCGWindowNumber] unsignedIntValue]==wid;}
 return NO;
}
int main(int argc,char **argv){@autoreleasepool{
 if(argc<2)die("Usage: mirror_events info | tap X Y | home|app-switcher|spotlight | scroll|magnify|rotate|translate|swipe DELTA [--send] [--pid-route|--minimal-record (tap only)] [--activate] [--sender ID] [--at X Y] [--prime-position|--prime-tap] [--tracking-region ID] [--no-phase] [--gesture-series] [--double (tap only)] [--horizontal (scroll only)] [--no-front]");
 NSString *action=@(argv[1]); BOOL send=NO,pidRoute=NO,hasSender=NO,primePosition=NO,primeTap=NO,hasAt=NO,noPhase=NO,gestureSeries=NO,activate=NO,minimalRecord=NO,doubleTap=NO,horizontal=NO,noFront=NO; int64_t sender=0;double atX=.5,atY=.55;
 int64_t trackingRegion=0;
 BOOL info=[action isEqual:@"info"];
 BOOL tapAction=[action isEqual:@"tap"],systemAction=[@[@"home",@"app-switcher",@"spotlight"] containsObject:action];
 if(info&&argc!=2)die("info takes no arguments.");
 if(tapAction){if(argc<4)die("tap requires normalized X Y.");atX=coordinate(argv[2]);atY=coordinate(argv[3]);hasAt=YES;}
 if(!info&&!tapAction&&!systemAction&&(![@[@"scroll",@"magnify",@"rotate",@"translate",@"swipe"] containsObject:action]||argc<3))die("Unknown action or missing delta.");
 for(int i=systemAction?2:(tapAction?4:3);i<argc;i++){
  if(!strcmp(argv[i],"--send")&&!send)send=YES;
  else if(!strcmp(argv[i],"--pid-route")&&!pidRoute)pidRoute=YES;
  else if(!strcmp(argv[i],"--minimal-record")&&!minimalRecord)minimalRecord=YES;
  else if(!strcmp(argv[i],"--double")&&!doubleTap)doubleTap=YES;
  else if(!strcmp(argv[i],"--horizontal")&&!horizontal)horizontal=YES;
  else if(!strcmp(argv[i],"--no-front")&&!noFront)noFront=YES;
  else if(!strcmp(argv[i],"--prime-position")&&!primePosition)primePosition=YES;
  else if(!strcmp(argv[i],"--prime-tap")&&!primeTap)primeTap=YES;
  else if(!strcmp(argv[i],"--no-phase")&&!noPhase)noPhase=YES;
  else if(!strcmp(argv[i],"--gesture-series")&&!gestureSeries)gestureSeries=YES;
  else if(!strcmp(argv[i],"--activate")&&!activate)activate=YES;
  else if(!strcmp(argv[i],"--tracking-region")&&!trackingRegion&&i+1<argc){
   char *regionEnd;const char *regionValue=argv[++i];errno=0;trackingRegion=strtoll(regionValue,&regionEnd,0);
   if(errno||regionEnd==regionValue||*regionEnd||trackingRegion<=0)die("Tracking region ID must be a positive signed 64-bit integer.");
  }
  else if(!strcmp(argv[i],"--at")&&!hasAt&&i+2<argc){
   char *coordEnd;const char *x=argv[++i];errno=0;atX=strtod(x,&coordEnd);
   if(errno||coordEnd==x||*coordEnd||!isfinite(atX)||atX<0||atX>1)die("Invalid normalized x coordinate.");
   const char *y=argv[++i];errno=0;atY=strtod(y,&coordEnd);
   if(errno||coordEnd==y||*coordEnd||!isfinite(atY)||atY<0||atY>1)die("Invalid normalized y coordinate.");
   hasAt=YES;
  }
  else if(!strcmp(argv[i],"--sender")&&!hasSender&&i+1<argc){
   char *senderEnd;const char *value=argv[++i];errno=0;sender=strtoll(value,&senderEnd,0);
   if(errno||senderEnd==value||*senderEnd||sender<=0)die("Sender ID must be a positive signed 64-bit integer.");
   hasSender=YES;
  }else die("Unknown, duplicate, or incomplete option.");
 }
 char *end;double value=0;
 if(primePosition&&primeTap)die("Choose one priming method.");
 if(trackingRegion&&!primePosition)die("--tracking-region requires --prime-position and a previously observed region ID for this window.");
 if(minimalRecord&&(!tapAction||pidRoute))die("--minimal-record requires tap with the record route.");
 if(doubleTap&&!tapAction)die("--double applies only to tap.");
 if(horizontal&&![action isEqual:@"scroll"])die("--horizontal applies only to scroll.");
 if(noFront&&activate)die("--no-front cannot be combined with --activate.");
 if(!info&&!tapAction&&!systemAction){errno=0;value=strtod(argv[2],&end);if(errno||end==argv[2]||*end||!isfinite(value)||fabs(value)>1000)die("Invalid delta.");}
 if((tapAction||systemAction)&&(primePosition||primeTap||gestureSeries||noPhase||hasSender))die("Tap/navigation do not accept gesture or sender options.");
 if(systemAction&&hasAt)die("Navigation does not take a position.");
 if(noPhase&&![action isEqual:@"scroll"])die("--no-phase applies only to scroll.");
 if(gestureSeries&&![ @[@"magnify",@"rotate",@"translate"] containsObject:action])die("--gesture-series applies only to magnify, rotate or translate.");
 NSString *bundle=@"com.apple.ScreenContinuity";
 NSString *title=@"iPhone Mirroring";
 NSArray *apps=[NSRunningApplication runningApplicationsWithBundleIdentifier:bundle];
 if(apps.count!=1)die("Expected one running iPhone Mirroring app.");
 pid_t pid=((NSRunningApplication *)apps[0]).processIdentifier;CGWindowID wid=0;CGRect b=CGRectZero;int matches=0;
 NSArray *windows=CFBridgingRelease(CGWindowListCopyWindowInfo(kCGWindowListOptionAll,kCGNullWindowID));
 for(NSDictionary *w in windows){
  if([w[(__bridge NSString *)kCGWindowOwnerPID] intValue]!=pid||![w[(__bridge NSString *)kCGWindowName] isEqual:title])continue;
  CGRect r;if(!CGRectMakeWithDictionaryRepresentation((__bridge CFDictionaryRef)w[(__bridge NSString *)kCGWindowBounds],&r)||r.size.height<100||r.size.width<100)continue;
  wid=[w[(__bridge NSString *)kCGWindowNumber] unsignedIntValue];b=r;matches++;
 }
 if(matches!=1)die("Expected one named main window; check Screen Recording access.");
 if(info){printf("{\"pid\":%d,\"window\":%u,\"x\":%.2f,\"y\":%.2f,\"width\":%.2f,\"height\":%.2f}\n",pid,wid,b.origin.x,b.origin.y,b.size.width,b.size.height);return 0;}
 if(send && !tapAction&&!systemAction&&![action isEqual:@"scroll"] && ![action isEqual:@"swipe"] && sender==0)die("Gesture submission requires --sender with a real local HID service ID; Mirroring drops unknown senders.");
 if([action isEqual:@"swipe"]&&(atY+value<0||atY+value>1||fabs(value)>1||primeTap||primePosition||gestureSeries))die("Swipe must stay within window bounds and cannot use priming or gesture series.");
 if((([action isEqual:@"magnify"]||[action isEqual:@"translate"])&&fabs(value)>1)||([action isEqual:@"rotate"]&&fabs(value)>360))die("Magnify/translation deltas must be in [-1,1]; rotation must be within 360 degrees.");
 void *sky=dlopen("/System/Library/PrivateFrameworks/SkyLight.framework/SkyLight",RTLD_NOW);
 void *app=dlopen("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices",RTLD_NOW);
 int(*getRecord)(CGEventRef,void*,size_t)=dlsym(sky,"SLEventGetEventRecord");
 void(*setWindowLocation)(CGEventRef,CGPoint)=dlsym(sky,"SLEventSetWindowLocation");
 int(*getPSN)(pid_t,PSN*)=dlsym(app,"GetProcessForPID");
 int(*front)(PSN*,uint32_t,uint32_t)=dlsym(sky,"_SLPSSetFrontProcessWithOptions");
 int(*post)(PSN*,void*)=dlsym(sky,"SLPSPostEventRecordTo");
 if(!getRecord||!setWindowLocation||!getPSN||!front||!post)die("Private APIs unavailable.");
 PSN psn={0};if(getPSN(pid,&psn))die("Could not address target process.");
 if(send&&activate){
  // Private front-process preparation can report "active" on another Space.
  // Always request normal activation and verify the actual target window too.
  [(NSRunningApplication *)apps[0] activateWithOptions:NSApplicationActivateAllWindows];
  NSDate *deadline=[NSDate dateWithTimeIntervalSinceNow:2];
  CGRect prior=CGRectNull,current;int stable=0;
  while(deadline.timeIntervalSinceNow>0){
   if([NSWorkspace sharedWorkspace].frontmostApplication.processIdentifier==pid&&windowBoundsOnScreen(wid,&current)){
    stable=CGRectEqualToRect(current,prior)?stable+1:0;prior=current;
    if(stable>=4){b=current;break;}
   }else{stable=0;prior=CGRectNull;}
   [[NSRunLoop currentRunLoop] runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.05]];
  }
  if(stable<4)die("Target did not become active with stable on-screen window bounds; no events sent.");
 }
 if(send&&!activate&&!noFront){int status=front(&psn,wid,0x200);if(status)die("Could not prepare target.");}
 int errors=0,count=tapAction?(doubleTap?4:2):(systemAction?2:14),primeCount=primeTap?2:(primePosition?1:0),builtCount=0;BOOL trackingExitSkipped=NO;
 for(int i=-primeCount-(gestureSeries?1:0)-(trackingRegion?1:0);i<count+(gestureSeries?1:0)+(trackingRegion?1:0);i++){
  int phase=i==0?1:(i==count-1?4:2);double delta=(i==0||i==count-1)?0:value/(count-2);CGEventRef e;
  BOOL seriesEvent=gestureSeries&&(i==-primeCount-1||i==count);
  BOOL trackingEvent=trackingRegion&&(i==-primeCount-(gestureSeries?1:0)-1||i==count+(gestureSeries?1:0));
  // Preserve genuine hover if the user moved the physical pointer into this window.
  if(send&&trackingEvent&&i>=count&&cursorOverWindow(wid)){trackingExitSkipped=YES;continue;}
  if(trackingEvent){
   NSEvent *enterExit=[NSEvent enterExitEventWithType:i<0?NSEventTypeMouseEntered:NSEventTypeMouseExited location:NSZeroPoint modifierFlags:0 timestamp:[NSProcessInfo processInfo].systemUptime windowNumber:wid context:nil eventNumber:0 trackingNumber:trackingRegion userData:NULL];
   e=CGEventCreateCopy(enterExit.CGEvent);
  }else if(systemAction){
   CGKeyCode key=[action isEqual:@"home"]?18:([action isEqual:@"app-switcher"]?19:20);
   e=CGEventCreateKeyboardEvent(NULL,key,i==0);CGEventSetFlags(e,kCGEventFlagMaskCommand);
  }else if(tapAction){
   e=CGEventCreateMouseEvent(NULL,(i%2==0)?kCGEventLeftMouseDown:kCGEventLeftMouseUp,CGPointZero,kCGMouseButtonLeft);
   CGEventSetIntegerValueField(e,kCGMouseEventClickState,i>=2?2:1);
   CGEventSetDoubleValueField(e,kCGMouseEventPressure,(i%2==0)?1:0);
  }else if(seriesEvent){
   e=CGEventCreate(NULL);CGEventSetType(e,(CGEventType)29);CGEventSetIntegerValueField(e,(CGEventField)55,29);
   CGEventSetIntegerValueField(e,(CGEventField)110,i<0?61:62);
   CGEventSetIntegerValueField(e,(CGEventField)132,i<0?1:4);
   CGEventSetIntegerValueField(e,(CGEventField)117,[action isEqual:@"magnify"]?8:([action isEqual:@"rotate"]?5:4));
  }else if([action isEqual:@"swipe"]){
   CGEventType pointerType=i==0?kCGEventLeftMouseDown:(i==count-1?kCGEventLeftMouseUp:kCGEventLeftMouseDragged);
   e=CGEventCreateMouseEvent(NULL,pointerType,CGPointZero,kCGMouseButtonLeft);
   CGEventSetIntegerValueField(e,kCGMouseEventClickState,1);
   CGEventSetDoubleValueField(e,kCGMouseEventPressure,i==count-1?0:1);
  }else if(i<0){
   CGEventType pointerType=primeTap?(i==-2?kCGEventLeftMouseDown:kCGEventLeftMouseUp):kCGEventMouseMoved;
   e=CGEventCreateMouseEvent(NULL,pointerType,CGPointZero,kCGMouseButtonLeft);
   if(primeTap)CGEventSetIntegerValueField(e,kCGMouseEventClickState,1);
  }else if([action isEqual:@"scroll"]){
   e=CGEventCreateScrollWheelEvent(NULL,kCGScrollEventUnitPixel,2,horizontal?0:(int32_t)llround(delta),horizontal?(int32_t)llround(delta):0);
   CGEventSetIntegerValueField(e,kCGScrollWheelEventIsContinuous,1);CGEventSetIntegerValueField(e,kCGScrollWheelEventScrollPhase,noPhase?0:phase);CGEventSetIntegerValueField(e,kCGScrollWheelEventMomentumPhase,0);
   // A nonzero sender selects these private fields in UniversalHIDKit.
   CGEventSetIntegerValueField(e,(CGEventField)173,0);CGEventSetIntegerValueField(e,(CGEventField)137,0);
   CGEventSetDoubleValueField(e,(CGEventField)175,horizontal?delta:0);CGEventSetDoubleValueField(e,(CGEventField)176,horizontal?0:delta);
   CGEventSetDoubleValueField(e,(CGEventField)177,horizontal?delta:0);CGEventSetDoubleValueField(e,(CGEventField)178,horizontal?0:delta);
  }else{
   e=CGEventCreate(NULL);CGEventSetType(e,(CGEventType)29);CGEventSetIntegerValueField(e,(CGEventField)55,29);
   int hid=[action isEqual:@"magnify"]?8:([action isEqual:@"rotate"]?5:4);
   CGEventSetIntegerValueField(e,(CGEventField)110,hid);CGEventSetIntegerValueField(e,(CGEventField)132,phase);
   CGEventSetDoubleValueField(e,(CGEventField)(hid==8?113:(hid==5?114:120)),delta);
  }
  CGEventSetTimestamp(e,clock_gettime_nsec_np(CLOCK_UPTIME_RAW));CGEventSetIntegerValueField(e,(CGEventField)87,trackingEvent?0:((i<0&&!seriesEvent&&!trackingRegion)?0:sender));
  double eventY=atY;
  if([action isEqual:@"swipe"])eventY+=value*fmin(1.,fmax(0.,(double)i/(count-2)));
  CGPoint local=CGPointMake(b.size.width*atX,b.size.height*eventY);CGEventSetLocation(e,CGPointMake(b.origin.x+local.x,b.origin.y+local.y));setWindowLocation(e,local);
  CGEventSetIntegerValueField(e,(CGEventField)51,wid);CGEventSetIntegerValueField(e,(CGEventField)91,wid);CGEventSetIntegerValueField(e,(CGEventField)92,wid);
  unsigned char record[248]={0};int r=getRecord(e,record,sizeof(record));uint32_t length=0;memcpy(&length,record+4,4);
  if(r||length!=sizeof(record))die("Unexpected event-record ABI; refusing dispatch.");
  record[0x3a]|=0x10;memcpy(record+0x3c,&wid,4);
  if(minimalRecord){
   // Match phone-harness background._post exactly. Generated records carry
   // extra event state and failed the off-Space tap that this path delivered.
   memset(record,0,sizeof(record));uint32_t recordLength=sizeof(record);
   memcpy(record+4,&recordLength,4);record[8]=(i%2==0)?1:2;record[0x3a]=0x10;
   memcpy(record+0x3c,&wid,4);
   CGPoint global=CGPointMake(b.origin.x+local.x,b.origin.y+local.y);
   memcpy(record+0x10,&global,sizeof(global));memcpy(record+0x20,&local,sizeof(local));
  }
  if(send){
   if(minimalRecord&&!noFront){int status=front(&psn,wid,0x200);if(status)die("Could not prepare target for tap edge.");}
   int status=0;if(pidRoute)CGEventPostToPid(pid,e);else{status=post(&psn,record);if(status)errors++;}
  }
  builtCount++;CFRelease(e);if(send)usleep(trackingEvent&&i<0?100000:(minimalRecord?50000:16000));
 }
 NSDictionary *result=@{@"action":action,@"delta":@(value),@"submitted":@(send),@"verified":@NO,@"route":pidRoute?@"pid":@"record",@"pid":@(pid),@"window":@(wid),@"sender":@(sender),@"prime_position":@(primePosition),@"prime_tap":@(primeTap),@"no_phase":@(noPhase),@"gesture_series":@(gestureSeries),@"tracking_region":@(trackingRegion),@"tracking_exit_skipped":@(trackingExitSkipped),@"at":@[@(atX),@(atY)],@"events":@(builtCount),@"dispatch_errors":@(errors)};
 NSMutableDictionary *out=[result mutableCopy];out[@"pointer_encoding"]=minimalRecord?@"minimal-record":@"generated";out[@"activate_requested"]=@(activate);out[@"double"]=@(doubleTap);out[@"horizontal"]=@(horizontal);out[@"no_front"]=@(noFront);
 NSData *data=[NSJSONSerialization dataWithJSONObject:out options:NSJSONWritingSortedKeys error:nil];puts([[NSString alloc]initWithData:data encoding:NSUTF8StringEncoding].UTF8String);return errors?1:0;
}}
